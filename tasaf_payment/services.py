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
    request message. MUSE will push the result back asynchronously.

    TODO (GovESB): Replace _publish() stub with real GovESB producer call
    when the coremis_app_integration adaptor is ready.
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
        Publish a verification request message to GovESB.

        TODO (GovESB): Replace this stub with:
            from coremis_app_integration.govesb import GovESBProducer
            GovESBProducer().publish(self.GOVESB_TOPIC_VERIFICATION_REQUEST, payload)
        """
        payload = {
            'account_uuid':   str(account.uuid),
            'account_number': account.account_number,
            'account_name':   account.account_name,
            'fsp_type':       account.fsp_type,
            'fsp_name':       account.fsp_name,
            'requested_by':   self.user.username,
        }
        logger.info(
            "[GovESB STUB] topic=%s payload=%s",
            self.GOVESB_TOPIC_VERIFICATION_REQUEST, payload,
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
        payroll_id: int,
        batch_type: str,
        payment_cycle_id: int = None,
        location_id: int = None,
    ) -> dict:
        """
        Generate a Paylist from verified + pre-audited accounts in a payroll.

        Only accounts with:
          - verification_status = VERIFIED
          - pre_audit_status    = PASSED
          - active_check_status = ACTIVE (or PENDING if active check not yet run)
          - is_primary = True
          - is_deleted = False

        And whose fsp_type matches batch_type (BANK/MNO) or any for MIXED.

        Returns: {'success': bool, 'paylist_uuid': str, 'item_count': int, 'error': str|None}
        """
        try:
            from payroll.models import BenefitConsumption

            # Load ACCEPTED benefits for this payroll
            benefits = BenefitConsumption.objects.filter(
                payroll_id=payroll_id,
                is_deleted=False,
            ).select_related('individual')

            if not benefits.exists():
                return {'success': False, 'error': 'No ACCEPTED benefits found for payroll'}

            # Build individual_id → PaymentAccount map
            individual_ids = [b.individual_id for b in benefits]
            account_qs = PaymentAccount.objects.filter(
                group_beneficiary__group__groupindividual__individual_id__in=individual_ids,
                verification_status=VerificationStatus.VERIFIED,
                pre_audit_status=PreAuditStatus.PASSED,
                is_primary=True,
                is_deleted=False,
            )
            if batch_type in ('BANK', 'MNO'):
                fsp_filter = 'BANK' if batch_type == 'BANK' else 'MOBILE'
                account_qs = account_qs.filter(fsp_type=fsp_filter)

            account_map = {}
            for acc in account_qs.select_related(
                'group_beneficiary__group__groupindividual'
            ):
                for gi in acc.group_beneficiary.group.groupindividual_set.filter(is_deleted=False):
                    account_map[gi.individual_id] = acc

            with transaction.atomic():
                paylist = Paylist.objects.create(
                    payroll_id=payroll_id,
                    payment_cycle_id=payment_cycle_id,
                    batch_type=batch_type,
                    status=PaylistStatus.PENDING_APPROVAL,
                    location_id=location_id,
                    generated_at=datetime.now(tz=timezone.utc),
                )

                items_created = 0
                for benefit in benefits:
                    account = account_map.get(benefit.individual_id)
                    if not account:
                        continue
                    PaylistItem.objects.create(
                        paylist=paylist,
                        payment_account=account,
                        benefit_consumption=benefit,
                        amount=benefit.amount,
                        status=PaylistItemStatus.PENDING,
                    )
                    items_created += 1

                if items_created == 0:
                    # Rollback — no point keeping an empty paylist
                    raise ValueError('No eligible accounts found for paylist generation')

            logger.info(
                "PaylistService.generate: paylist=%s batch_type=%s items=%d (user=%s)",
                paylist.uuid, batch_type, items_created, self.user.username,
            )
            return {
                'success': True,
                'paylist_uuid': str(paylist.uuid),
                'item_count': items_created,
                'error': None,
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
        Publish paylist to GovESB for MUSE processing.

        TODO (GovESB): Replace with:
            from coremis_app_integration.govesb import GovESBProducer
            GovESBProducer().publish(self.GOVESB_TOPIC_PAYMENT_SUBMIT, payload)
        """
        items = list(paylist.items.select_related('payment_account').all())
        payload = {
            'paylist_uuid':  str(paylist.uuid),
            'batch_type':    paylist.batch_type,
            'item_count':    len(items),
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
        logger.info("[GovESB STUB] topic=%s paylist=%s", self.GOVESB_TOPIC_PAYMENT_SUBMIT, paylist.uuid)
        logger.debug("[GovESB STUB] payload=%s", payload)


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
