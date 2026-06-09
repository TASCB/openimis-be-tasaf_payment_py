"""
tasaf_payment.services
=======================
Business logic for payment account registration and MUSE-driven verification.

Architecture notes
------------------
*   PaymentAccountService         — standard openIMIS CRUD service
*   MuseVerificationDispatchService — marks accounts as PENDING_MUSE and publishes
                                      a verification request to GovESB (stubbed
                                      until the GovESB adaptor is available)
*   MuseVerificationInboundService  — handles the result payload pushed back from
                                      MUSE via GovESB; creates MuseVerificationRecord
                                      and updates PaymentAccount status
*   ManualApprovalService         — approve / reject MANUAL-status accounts
*   PreAuditService               — pre-audit and claims validation checks
*   PaylistService                — generate, approve and submit paylists
*   ReturnFeedbackService         — receive and store return/unapplied feedback

MUSE verification flow (new)
-----------------------------
1.  Operator selects accounts in UI → runVerification mutation.
2.  MuseVerificationDispatchService.dispatch(account_ids):
        a.  Set PaymentAccount.verification_status = PENDING_MUSE.
        b.  Publish verification request to GovESB (stub: logs only).
3.  MUSE performs FSP/account verification via TIPS + mobile validation.
4.  MUSE pushes result to GovESB → TASAF MIS consumes it.
5.  MuseVerificationInboundService.handle_result(payload):
        a.  Validate and parse payload.
        b.  Update PaymentAccount.verification_status (VERIFIED/FAILED/MANUAL).
        c.  Store muse_verification_reference.
        d.  Create MuseVerificationRecord for audit trail.

TODO (GovESB): Replace stub in MuseVerificationDispatchService._publish()
and wire MuseVerificationInboundService.handle_result() to the GovESB
consumer in coremis_app_integration when the adaptor is available.
"""

import logging
import uuid
from datetime import datetime, timezone

from django.db import transaction

from core.services import BaseService
from core.signals import register_service_signal
from core.services.utils import output_exception, check_authentication
from tasaf_payment.models import (
    PaymentAccount,
    VerificationStatus,
    PreAuditStatus,
    ActiveCheckStatus,
    PaylistStatus,
    PaylistItemStatus,
    MuseVerificationRecord,
    MuseVerificationType,
    Paylist,
    PaylistItem,
    ReturnFeedback,
    ReturnFeedbackType,
)
from tasaf_payment.validation import PaymentAccountValidation

logger = logging.getLogger(__name__)


def _govesb_publish(topic: str, payload: dict, *, user_id=None, context: str = "") -> dict:
    """
    Send ``payload`` to MUSE over the shared GovESB transport
    (``coremis_app_integration.govesb.GovESBProducer``).

    This is deliberately fail-soft: if the coremis adaptor is not installed, or
    GovESB is disabled/misconfigured, or the send raises, it logs and returns a
    result dict instead of propagating — so the surrounding payment-state
    transitions (PENDING_MUSE, SUBMITTED, ...) still commit in environments
    without live ESB credentials, exactly as the previous stub behaved. The
    ESB-disabled case is itself a clean no-op inside the producer.
    """
    try:
        from coremis_app_integration.govesb import GovESBProducer
    except ImportError:
        logger.info("[GovESB] adaptor unavailable; skipped topic=%s %s", topic, context)
        return {"published": False, "unavailable": True}

    try:
        result = GovESBProducer().publish(topic, payload, user_id=user_id)
        if not result.get("published"):
            logger.info("[GovESB] not sent (disabled) topic=%s %s", topic, context)
        return result
    except Exception:  # noqa: BLE001 — never let dispatch transport break the flow
        logger.exception("[GovESB] publish failed topic=%s %s", topic, context)
        return {"published": False, "error": True}


# ─── PaymentAccountService ────────────────────────────────────────────────────

class PaymentAccountService(BaseService):
    """
    Standard openIMIS CRUD service for PaymentAccount.

    Emits register_service_signal hooks on create/update/delete so other
    modules can listen (e.g. to enforce business rules without modifying
    this module).
    """

    OBJECT_TYPE = PaymentAccount

    def __init__(self, user, validation_class=PaymentAccountValidation):
        super().__init__(user, validation_class)

    @register_service_signal('payment_account_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('payment_account_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('payment_account_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)


# ─── MuseVerificationDispatchService ─────────────────────────────────────────

class MuseVerificationDispatchService:
    """
    Sends payment accounts to MUSE for verification via GovESB.

    Sets each account to PENDING_MUSE status and publishes a verification
    request message over GovESB. MUSE pushes the result back asynchronously to
    the inbound endpoint (handled by MuseVerificationInboundService).
    """

    GOVESB_TOPIC_VERIFICATION_REQUEST = 'tasaf.verification.request'

    def __init__(self, user):
        self.user = user

    @check_authentication
    def dispatch(self, account_ids: list) -> dict:
        """
        Mark accounts as PENDING_MUSE and publish verification request to GovESB.

        Returns: {'success': bool, 'count': int, 'error': str|None}
        """
        try:
            with transaction.atomic():
                accounts = list(
                    PaymentAccount.objects.filter(
                        id__in=account_ids,
                        is_deleted=False,
                    ).select_related('group_beneficiary')
                )
                if not accounts:
                    return {'success': True, 'count': 0, 'error': None}

                ids_to_dispatch = []
                for account in accounts:
                    account.verification_status = VerificationStatus.PENDING_MUSE
                    account.save(username=self.user.username)
                    ids_to_dispatch.append(account.id)

            # Publish outside the atomic block so DB commit is visible to consumer
            for account in accounts:
                self._publish(account)

            logger.info(
                "MuseVerificationDispatchService.dispatch: %d accounts sent (user=%s)",
                len(ids_to_dispatch), self.user.username,
            )
            return {'success': True, 'count': len(ids_to_dispatch), 'error': None}

        except Exception as exc:
            logger.exception("MuseVerificationDispatchService.dispatch failed")
            return output_exception(
                model_name="PaymentAccount",
                method="dispatch_verification",
                exception=exc,
            )

    def _publish(self, account: PaymentAccount) -> None:
        """
        Publish a verification request to MUSE over the shared GovESB transport.

        Fail-soft (see :func:`_govesb_publish`): when GovESB is unavailable or
        disabled the account still stays in PENDING_MUSE — the request simply
        is not transmitted until ESB credentials are configured.
        """
        payload = {
            'account_uuid':   str(account.uuid),
            'account_number': account.account_number,
            'account_name':   account.account_name,
            'fsp_type':       account.fsp_type,
            'fsp_name':       account.fsp_name,
            'requested_by':   self.user.username,
        }
        _govesb_publish(
            self.GOVESB_TOPIC_VERIFICATION_REQUEST,
            payload,
            user_id=getattr(self.user, 'username', None),
            context=f"verification account={account.uuid}",
        )


# ─── MuseVerificationInboundService ──────────────────────────────────────────

class MuseVerificationInboundService:
    """
    Processes verification results pushed from MUSE via GovESB.

    Called by:
    - The stub REST endpoint (POST /api/tasaf_payment/muse/verification_result/)
      for development/testing.
    - The GovESB consumer in coremis_app_integration (when available).

    Expected payload:
    {
        "account_uuid":       "...",
        "muse_reference":     "MUSE-REF-123",
        "verification_type":  "FSP_ACCOUNT" | "MOBILE_VALIDATION" | "ACTIVE_CHECK",
        "result":             "PASSED" | "FAILED" | "MANUAL",
        "failure_reason":     "..." | null,
        "raw_response":       {...}
    }
    """

    # Map MUSE result string to VerificationStatus int
    _RESULT_TO_STATUS = {
        'PASSED': VerificationStatus.VERIFIED,
        'FAILED': VerificationStatus.FAILED,
        'MANUAL': VerificationStatus.MANUAL,
    }

    def handle_result(self, payload: dict) -> dict:
        """
        Parse MUSE result payload, update PaymentAccount, create audit record.

        Returns: {'success': bool, 'account_uuid': str, 'error': str|None}
        """
        account_uuid = payload.get('account_uuid')
        try:
            account = PaymentAccount.objects.get(uuid=account_uuid, is_deleted=False)
        except PaymentAccount.DoesNotExist:
            logger.error("MuseVerificationInboundService: unknown account_uuid=%s", account_uuid)
            return {'success': False, 'account_uuid': account_uuid,
                    'error': f"PaymentAccount {account_uuid} not found"}

        result_str = payload.get('result', '').upper()
        new_status = self._RESULT_TO_STATUS.get(result_str)
        if new_status is None:
            return {'success': False, 'account_uuid': account_uuid,
                    'error': f"Unknown result value: {result_str}"}

        muse_ref = payload.get('muse_reference', '')
        v_type = payload.get('verification_type', MuseVerificationType.FSP_ACCOUNT)

        try:
            with transaction.atomic():
                # Update account
                account.verification_status = new_status
                account.muse_verification_reference = muse_ref
                account.save()

                # If this is an active check result, update active_check_status too
                if v_type == MuseVerificationType.ACTIVE_CHECK:
                    account.active_check_status = (
                        ActiveCheckStatus.ACTIVE
                        if result_str == 'PASSED'
                        else ActiveCheckStatus.INACTIVE
                    )
                    account.save()

                # Immutable audit record
                MuseVerificationRecord.objects.create(
                    payment_account=account,
                    muse_reference=muse_ref,
                    verification_type=v_type,
                    result=result_str,
                    failure_reason=payload.get('failure_reason'),
                    raw_response=payload.get('raw_response', {}),
                )

            logger.info(
                "MuseVerificationInboundService.handle_result: account=%s result=%s ref=%s",
                account_uuid, result_str, muse_ref,
            )
            return {'success': True, 'account_uuid': account_uuid, 'error': None}

        except Exception as exc:
            logger.exception(
                "MuseVerificationInboundService.handle_result failed for account=%s",
                account_uuid,
            )
            return output_exception(
                model_name="PaymentAccount",
                method="handle_verification_result",
                exception=exc,
            )


# ─── ManualApprovalService ────────────────────────────────────────────────────

class ManualApprovalService:
    """
    Approve or reject accounts that MUSE returned as MANUAL (borderline).

    approved=True  → VERIFIED  (cleared for payment)
    approved=False → FAILED    (must resubmit with corrected data)

    Writes reviewer identity and notes into json_ext for audit trail.
    """

    def __init__(self, user):
        self.user = user

    @check_authentication
    def approve_accounts(self, account_ids: list, approved: bool, review_notes: str = '') -> dict:
        try:
            with transaction.atomic():
                target_status = (
                    VerificationStatus.VERIFIED if approved
                    else VerificationStatus.FAILED
                )
                accounts = PaymentAccount.objects.filter(
                    id__in=account_ids,
                    verification_status=VerificationStatus.MANUAL,
                    is_deleted=False,
                )
                count = accounts.count()
                for account in accounts:
                    account.verification_status = target_status
                    ext = account.json_ext or {}
                    ext['review_notes']    = review_notes
                    ext['reviewed_by']     = self.user.username
                    ext['review_decision'] = 'approved' if approved else 'rejected'
                    account.json_ext = ext
                    account.save(username=self.user.username)

            return {'success': True, 'count': count, 'error': None}

        except Exception as exc:
            logger.exception("ManualApprovalService.approve_accounts failed")
            return output_exception(
                model_name="PaymentAccount",
                method="approve_accounts",
                exception=exc,
            )


# ─── PreAuditService ──────────────────────────────────────────────────────────

class PreAuditService:
    """
    Pre-audit and claims validation checks for verified accounts.

    Runs business rule checks before a paylist can be generated.
    Accounts must be VERIFIED and pass all checks to reach pre_audit_status=PASSED.

    Current checks:
    - Account must be VERIFIED (not PENDING / FAILED / MANUAL)
    - Account must be is_primary=True
    - Account must have a linked active GroupBeneficiary

    Additional claims validation rules can be added here without touching
    other modules — same signal/hook pattern as PaymentAccountService.
    """

    def __init__(self, user):
        self.user = user

    @check_authentication
    def run_pre_audit(self, account_ids: list) -> dict:
        """
        Run pre-audit checks on the supplied account IDs.

        Returns: {'success': bool, 'passed': int, 'failed': int, 'error': str|None}
        """
        try:
            accounts = PaymentAccount.objects.filter(
                id__in=account_ids,
                is_deleted=False,
            ).select_related('group_beneficiary')

            passed = 0
            failed = 0
            with transaction.atomic():
                for account in accounts:
                    reasons = self._check_account(account)
                    if reasons:
                        account.pre_audit_status = PreAuditStatus.FAILED
                        ext = account.json_ext or {}
                        ext['pre_audit_failures'] = reasons
                        account.json_ext = ext
                        failed += 1
                    else:
                        account.pre_audit_status = PreAuditStatus.PASSED
                        failed_key = 'pre_audit_failures'
                        if failed_key in (account.json_ext or {}):
                            del account.json_ext[failed_key]
                        passed += 1
                    account.save(username=self.user.username)

            return {
                'success': True,
                'passed': passed,
                'failed': failed,
                'error': None,
            }

        except Exception as exc:
            logger.exception("PreAuditService.run_pre_audit failed")
            return output_exception(
                model_name="PaymentAccount",
                method="run_pre_audit",
                exception=exc,
            )

    @staticmethod
    def _check_account(account: PaymentAccount) -> list:
        """
        Return a list of failure reasons for this account.
        Empty list means the account passes pre-audit.
        """
        reasons = []
        if account.verification_status != VerificationStatus.VERIFIED:
            reasons.append('Account is not VERIFIED')
        if not account.is_primary:
            reasons.append('Account is not marked as primary')
        if account.group_beneficiary is None:
            reasons.append('Account has no linked GroupBeneficiary')
        elif account.group_beneficiary.status != 'ACTIVE':
            reasons.append('GroupBeneficiary is not ACTIVE')
        return reasons


# ─── PaylistService ───────────────────────────────────────────────────────────

class PaylistService:
    """
    Generate, approve, and submit paylists to MUSE via GovESB.

    One payroll can produce multiple paylists (e.g., one Bank + one MNO).
    The paylist is the TASAF-specific dispatch batch — not the raw payroll.

    generate():  Build Paylist + PaylistItems from verified + pre-audited accounts.
    approve():   Move paylist from PENDING_APPROVAL → APPROVED.
    submit():    Move paylist from APPROVED → SUBMITTED and publish to GovESB.

    TODO (GovESB): Replace _publish_paylist() stub with real GovESB producer.
    """

    GOVESB_TOPIC_PAYMENT_SUBMIT = 'tasaf.payment.submit'

    def __init__(self, user):
        self.user = user

    @check_authentication
    def generate(
        self,
        payroll_id,            # UUID — Payroll PK
        batch_type: str,
        payment_cycle_id=None,  # UUID — PaymentCycle PK
        location_id: int = None,
    ) -> dict:
        """
        Generate one or more Paylists from verified + pre-audited accounts.

        Thin dispatcher: for large payrolls (eligible benefits >
        ``TasafPaymentConfig.paylist_async_threshold``) the work is handed to the
        ``generate_paylists_task`` Celery task so the request returns immediately;
        otherwise it runs inline. If enqueuing fails (e.g. no broker in dev) it
        falls back to running inline. The heavy lifting lives in
        :meth:`_generate_sync`.
        """
        try:
            from payroll.models import (
                BenefitConsumption,
                BenefitConsumptionStatus,
                PayrollBenefitConsumption,
            )
            from tasaf_payment.apps import TasafPaymentConfig

            # Cheap size probe — count of ACCEPTED benefits on the payroll.
            benefit_count = BenefitConsumption.objects.filter(
                id__in=PayrollBenefitConsumption.objects.filter(
                    payroll_id=payroll_id, is_deleted=False,
                ).values_list('benefit_id', flat=True),
                status=BenefitConsumptionStatus.ACCEPTED,
                is_deleted=False,
            ).count()
            if benefit_count == 0:
                return {'success': False, 'error': 'No ACCEPTED benefits found for payroll'}

            try:
                threshold = int(TasafPaymentConfig.paylist_async_threshold or 0)
            except (TypeError, ValueError):
                threshold = 0

            if threshold and benefit_count > threshold:
                try:
                    from tasaf_payment.tasks import generate_paylists_task
                    generate_paylists_task.delay(
                        self.user.id,
                        str(payroll_id),
                        batch_type,
                        str(payment_cycle_id) if payment_cycle_id else None,
                        location_id,
                    )
                    logger.info(
                        "PaylistService.generate: queued async (benefits=%d > threshold=%d, payroll=%s)",
                        benefit_count, threshold, payroll_id,
                    )
                    return {
                        'success': True, 'error': None, 'queued': True,
                        'paylists': [], 'paylist_count': 0, 'total_items': 0,
                        'paylist_uuid': None, 'item_count': 0,
                    }
                except Exception:  # noqa: BLE001 — no broker / enqueue failure → run inline
                    logger.warning(
                        "PaylistService.generate: async enqueue failed, running inline",
                        exc_info=True,
                    )

            return self._generate_sync(payroll_id, batch_type, payment_cycle_id, location_id)

        except Exception as exc:
            logger.exception("PaylistService.generate failed")
            return output_exception(model_name="Paylist", method="generate", exception=exc)

    def _generate_sync(
        self,
        payroll_id,
        batch_type: str,
        payment_cycle_id=None,
        location_id: int = None,
    ) -> dict:
        """
        Build the Paylists + items (the heavy worker behind :meth:`generate`).

        Only accounts with verification_status=VERIFIED, pre_audit_status=PASSED,
        is_primary=True, is_deleted=False are included.

        Batching rule (MUSE): BANK and MNO are NEVER mixed in one batch, and each
        FSP's eligible accounts are split into Paylists of at most
        ``TasafPaymentConfig.paylist_max_batch_size`` transactions (default 50000;
        0/None = no cap). ``batch_type``: BANK → BANK batches; MNO → MNO batches;
        MIXED → both, each as its own single-FSP batches (never a mixed batch).
        Sibling batches from one FSP run share a ``batch_group`` UUID and carry
        ``batch_sequence`` (1..N) / ``batch_total`` (N).

        Line items are written with ``bulk_create`` (audit columns set explicitly)
        for throughput; this bypasses per-item simple_history rows by design — the
        Paylist header keeps full history, and items are immutable batch lines.

        Returns: {'success': bool, 'paylists': [{paylist_uuid, batch_type,
        batch_sequence, batch_total, item_count}], 'paylist_count': int,
        'total_items': int, 'paylist_uuid': str (first), 'item_count': int
        (total), 'error': str|None}
        """
        try:
            from payroll.models import (
                BenefitConsumption,
                BenefitConsumptionStatus,
                PayrollBenefitConsumption,
            )
            from tasaf_payment.apps import TasafPaymentConfig

            try:
                max_size = int(TasafPaymentConfig.paylist_max_batch_size or 0)
            except (TypeError, ValueError):
                max_size = 0
            if max_size < 0:
                max_size = 0

            user_pk = getattr(self.user, 'id', None)

            # (account.fsp_type, stored Paylist.batch_type) targets for this run.
            if batch_type == 'BANK':
                fsp_targets = [('BANK', 'BANK')]
            elif batch_type == 'MNO':
                fsp_targets = [('MOBILE', 'MNO')]
            else:  # MIXED → both FSPs, each in its own single-FSP batches
                fsp_targets = [('BANK', 'BANK'), ('MOBILE', 'MNO')]

            # BenefitConsumption has no direct payroll FK — the payroll↔benefit
            # relation lives on the PayrollBenefitConsumption join model.
            benefit_ids = PayrollBenefitConsumption.objects.filter(
                payroll_id=payroll_id,
                is_deleted=False,
            ).values_list('benefit_id', flat=True)

            benefits = list(BenefitConsumption.objects.filter(
                id__in=benefit_ids,
                status=BenefitConsumptionStatus.ACCEPTED,
                is_deleted=False,
            ).select_related('individual'))

            if not benefits:
                return {'success': False, 'error': 'No ACCEPTED benefits found for payroll'}

            individual_ids = [b.individual_id for b in benefits]
            created = []

            with transaction.atomic():
                for fsp_type, stored_type in fsp_targets:
                    account_qs = PaymentAccount.objects.filter(
                        group_beneficiary__group__groupindividual__individual_id__in=individual_ids,
                        verification_status=VerificationStatus.VERIFIED,
                        pre_audit_status=PreAuditStatus.PASSED,
                        is_primary=True,
                        is_deleted=False,
                        fsp_type=fsp_type,
                    ).select_related('group_beneficiary__group__groupindividual')

                    account_map = {}
                    for acc in account_qs:
                        for gi in acc.group_beneficiary.group.groupindividual_set.filter(is_deleted=False):
                            account_map[gi.individual_id] = acc

                    # Eligible (benefit, account) pairs, deterministically ordered
                    # so batch boundaries are reproducible and auditable.
                    pairs = [
                        (b, account_map[b.individual_id])
                        for b in benefits if b.individual_id in account_map
                    ]
                    pairs.sort(key=lambda p: ((p[1].account_number or ''), str(p[0].id)))
                    if not pairs:
                        continue

                    if max_size and len(pairs) > max_size:
                        chunks = [pairs[i:i + max_size] for i in range(0, len(pairs), max_size)]
                    else:
                        chunks = [pairs]

                    group_id = uuid.uuid4()
                    total = len(chunks)
                    now = datetime.now(tz=timezone.utc)
                    for seq, chunk in enumerate(chunks, start=1):
                        paylist = Paylist.objects.create(
                            payroll_id=payroll_id,
                            payment_cycle_id=payment_cycle_id,
                            batch_type=stored_type,
                            status=PaylistStatus.PENDING_APPROVAL,
                            location_id=location_id,
                            generated_at=now,
                            batch_group=group_id,
                            batch_sequence=seq,
                            batch_total=total,
                        )
                        # bulk_create the line items for throughput. Audit columns
                        # (id/version/is_deleted/dates/user) are set explicitly since
                        # bulk_create bypasses Model.save() and HistoryModel defaults.
                        item_objs = [
                            PaylistItem(
                                id=uuid.uuid4(),
                                paylist=paylist,
                                payment_account=account,
                                benefit_consumption=benefit,
                                amount=benefit.amount,
                                status=PaylistItemStatus.PENDING,
                                is_deleted=False,
                                version=1,
                                date_created=now,
                                date_updated=now,
                                user_created_id=user_pk,
                                user_updated_id=user_pk,
                            )
                            for benefit, account in chunk
                        ]
                        PaylistItem.objects.bulk_create(item_objs, batch_size=2000)
                        created.append({
                            'paylist_uuid':   str(paylist.uuid),
                            'batch_type':     stored_type,
                            'batch_sequence': seq,
                            'batch_total':    total,
                            'item_count':     len(chunk),
                        })

                if not created:
                    # Rollback — nothing eligible for any FSP target
                    raise ValueError('No eligible accounts found for paylist generation')

            total_items = sum(c['item_count'] for c in created)
            logger.info(
                "PaylistService.generate: payroll=%s batch_type=%s → %d paylist(s), %d item(s) (max_size=%s, user=%s)",
                payroll_id, batch_type, len(created), total_items, max_size or 'unlimited', self.user.username,
            )
            return {
                'success': True,
                'error': None,
                'paylists': created,
                'paylist_count': len(created),
                'total_items': total_items,
                # Back-compat single-paylist fields (first batch / total items).
                'paylist_uuid': created[0]['paylist_uuid'],
                'item_count': total_items,
            }

        except Exception as exc:
            logger.exception("PaylistService.generate failed")
            return output_exception(
                model_name="Paylist",
                method="generate",
                exception=exc,
            )

    @check_authentication
    def approve(self, paylist_uuid: str) -> dict:
        """Move paylist from PENDING_APPROVAL → APPROVED."""
        try:
            paylist = Paylist.objects.get(uuid=paylist_uuid, is_deleted=False)
            if paylist.status != PaylistStatus.PENDING_APPROVAL:
                return {'success': False, 'error': f'Paylist is {paylist.status}, not PENDING_APPROVAL'}

            paylist.status = PaylistStatus.APPROVED
            paylist.approved_at = datetime.now(tz=timezone.utc)
            paylist.save()

            logger.info("PaylistService.approve: paylist=%s (user=%s)", paylist_uuid, self.user.username)
            return {'success': True, 'error': None}

        except Paylist.DoesNotExist:
            return {'success': False, 'error': f'Paylist {paylist_uuid} not found'}
        except Exception as exc:
            logger.exception("PaylistService.approve failed")
            return output_exception(model_name="Paylist", method="approve", exception=exc)

    @check_authentication
    def submit(self, paylist_uuid: str) -> dict:
        """
        Move paylist from APPROVED → SUBMITTED and publish to GovESB.

        TODO (GovESB): Replace _publish_paylist() stub.
        """
        try:
            paylist = Paylist.objects.get(uuid=paylist_uuid, is_deleted=False)
            if paylist.status != PaylistStatus.APPROVED:
                return {'success': False, 'error': f'Paylist is {paylist.status}, not APPROVED'}

            paylist.status = PaylistStatus.SUBMITTED
            paylist.submitted_at = datetime.now(tz=timezone.utc)
            paylist.save()

            self._publish_paylist(paylist)

            logger.info("PaylistService.submit: paylist=%s (user=%s)", paylist_uuid, self.user.username)
            return {'success': True, 'error': None}

        except Paylist.DoesNotExist:
            return {'success': False, 'error': f'Paylist {paylist_uuid} not found'}
        except Exception as exc:
            logger.exception("PaylistService.submit failed")
            return output_exception(model_name="Paylist", method="submit", exception=exc)

    def _publish_paylist(self, paylist: Paylist) -> None:
        """
        Publish the approved paylist to MUSE over the shared GovESB transport.

        Fail-soft (see :func:`_govesb_publish`): when GovESB is unavailable or
        disabled the paylist still moves to SUBMITTED; the batch is simply not
        transmitted until ESB credentials are configured.
        """
        items = list(paylist.items.select_related('payment_account').all())
        payload = {
            'paylist_uuid':   str(paylist.uuid),
            'batch_type':     paylist.batch_type,
            'batch_group':    str(paylist.batch_group) if paylist.batch_group else None,
            'batch_sequence': paylist.batch_sequence,
            'batch_total':    paylist.batch_total,
            'item_count':     len(items),
            'items': [
                {
                    'item_uuid':      str(item.uuid),
                    'account_number': item.payment_account.account_number,
                    'fsp_name':       item.payment_account.fsp_name,
                    'fsp_type':       item.payment_account.fsp_type,
                    'amount':         str(item.amount),
                }
                for item in items
            ],
        }
        _govesb_publish(
            self.GOVESB_TOPIC_PAYMENT_SUBMIT,
            payload,
            user_id=getattr(self.user, 'username', None),
            context=f"paylist={paylist.uuid} items={len(items)}",
        )


# ─── ReturnFeedbackService ────────────────────────────────────────────────────

class ReturnFeedbackService:
    """
    Receives and stores return / unapplied feedback from MUSE via GovESB.

    Called by:
    - The stub REST endpoint (POST /api/tasaf_payment/muse/return_feedback/)
      for development/testing.
    - The GovESB consumer in coremis_app_integration (when available).

    Expected payload:
    {
        "paylist_item_uuid": "...",
        "feedback_type":     "UNAPPLIED" | "RETURNED" | "PARTIAL",
        "reason_code":       "...",
        "reason_description": "...",
        "muse_reference":    "..."
    }
    """

    GOVESB_TOPIC_RETURN_FEEDBACK = 'muse.payment.feedback'

    def handle_feedback(self, payload: dict) -> dict:
        item_uuid = payload.get('paylist_item_uuid')
        try:
            item = PaylistItem.objects.get(uuid=item_uuid)
        except PaylistItem.DoesNotExist:
            logger.error("ReturnFeedbackService: unknown paylist_item_uuid=%s", item_uuid)
            return {'success': False, 'error': f'PaylistItem {item_uuid} not found'}

        feedback_type = payload.get('feedback_type', '').upper()
        if feedback_type not in ReturnFeedbackType.values:
            return {'success': False, 'error': f'Unknown feedback_type: {feedback_type}'}

        try:
            with transaction.atomic():
                ReturnFeedback.objects.create(
                    paylist_item=item,
                    feedback_type=feedback_type,
                    reason_code=payload.get('reason_code'),
                    reason_description=payload.get('reason_description'),
                )
                # Update item status
                item.status = (
                    PaylistItemStatus.UNAPPLIED
                    if feedback_type == 'UNAPPLIED'
                    else PaylistItemStatus.RETURNED
                )
                item.return_reason = payload.get('reason_description')
                if payload.get('muse_reference'):
                    item.muse_reference = payload['muse_reference']
                item.save()

            logger.info(
                "ReturnFeedbackService.handle_feedback: item=%s type=%s",
                item_uuid, feedback_type,
            )
            return {'success': True, 'error': None}

        except Exception as exc:
            logger.exception("ReturnFeedbackService.handle_feedback failed for item=%s", item_uuid)
            return output_exception(
                model_name="PaylistItem",
                method="handle_feedback",
                exception=exc,
            )


# ─── Backward-compatible alias ────────────────────────────────────────────────

# Old code that imports VerificationService still works — it now points to the
# dispatch service for the run path and the approval service for the approve path.
# Callers that used _verify_single_account() directly must migrate to the new services.
class VerificationService(MuseVerificationDispatchService):
    """Deprecated alias. Use MuseVerificationDispatchService."""

    def run_verification(self, account_ids: list) -> dict:
        return self.dispatch(account_ids)

    def approve_accounts(self, account_ids: list, approved: bool, review_notes: str = '') -> dict:
        return ManualApprovalService(self.user).approve_accounts(account_ids, approved, review_notes)


class BatchVerificationService:
    """Deprecated alias. Use MuseVerificationDispatchService."""

    def __init__(self, user):
        self.user = user

    def dispatch(self, filters: dict) -> dict:
        from tasaf_payment.tasks import run_batch_verification_task
        try:
            async_result = run_batch_verification_task.delay(filters, self.user.id)
            return {'success': True, 'task_id': str(async_result.id), 'error': None}
        except Exception as exc:
            logger.exception("BatchVerificationService.dispatch failed")
            return output_exception(
                model_name="PaymentAccount",
                method="dispatch_batch_verification",
                exception=exc,
            )
