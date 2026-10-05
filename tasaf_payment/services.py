"""Business logic for TASAF payment accounts, MUSE verification, and paylists."""

import logging
import uuid
from datetime import datetime, timezone

from django.db import transaction
from django.utils.translation import gettext as _
from django.db.models import Q, Sum

from core.services import BaseService
from core.signals import register_service_signal
from core.services.utils import output_exception, check_authentication
from tasks_management.services import UpdateCheckerLogicServiceMixin
from tasaf_payment.validation import WithdrawalChargeValidation
from tasaf_payment.models import (
    WithdrawalCharge,
    PaymentAccount,
    VerificationStatus,
    PreAuditStatus,
    ActiveCheckStatus,
    PaylistStatus,
    PaylistItemStatus,
    PaymentDestination,
    PAYLIST_ITEM_TERMINAL_STATUSES,
    PAYLIST_IN_FLIGHT_STATUSES,
    MuseVerificationRecord,
    MuseVerificationType,
    Paylist,
    PaylistItem,
    ReturnFeedback,
    ReturnFeedbackType,
)
from tasaf_payment.validation import PaymentAccountValidation

logger = logging.getLogger(__name__)


def _inbound_audit_user(fallback_obj=None):
    """
    Resolve the user to attribute an inbound gateway write to.

    Gateway callbacks are machine-to-machine: there is no logged-in user, and
    ``HistoryModel.save()`` raises without one. Resolution order:

    1. ``get_current_user()`` — set when a real request context exists;
    2. ``TasafPaymentConfig.inbound_system_username`` — the configured service account;
    3. whoever created the record being updated, so the audit chain stays non-null.

    Returns None if none resolve; callers should then let the save raise rather than
    silently dropping the gateway's message.
    """
    try:
        from core.models import HistoryModel  # noqa: F401  (import guard only)
        from core.utils import get_current_user
        current = get_current_user()
        if current:
            return current
    except Exception:  # noqa: BLE001 — no request context is normal here
        pass

    from core.models import User
    from tasaf_payment.apps import TasafPaymentConfig

    username = getattr(TasafPaymentConfig, 'inbound_system_username', None)
    if username:
        user = User.objects.filter(username=username).first()
        if user:
            return user
        logger.warning(
            "tasaf_payment.inbound_system_username=%r does not match a user", username,
        )

    return getattr(fallback_obj, 'user_created', None) or getattr(fallback_obj, 'user_updated', None)


def _parse_dt(value):
    """Parse an ISO-8601 timestamp, returning None on anything unusable.

    Naive values are treated as UTC — the gateway sends wall-clock times.
    """
    if not value:
        return None
    try:
        from django.utils.dateparse import parse_datetime
        parsed = parse_datetime(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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


def _queue_or_inline(task, task_args, inline, count_ids, label, bounded):
    """Run a batch through Celery, falling back to inline when no broker is reachable.

    Dev has no broker; the dockerised PSSN stack does. The feature must work in both, so
    an enqueue failure is a fallback, not an error.

    *bounded* caps the inline path against
    ``TasafPaymentConfig.batch_inline_fallback_limit``. Use it wherever running inline
    would do outbound I/O -- a synchronous GovESB blast is worse than a clear refusal.
    Leave it off for DB-only work, where finishing matters more than latency.
    """
    from tasaf_payment.apps import TasafPaymentConfig

    try:
        async_result = task.delay(*task_args)
        return {'success': True, 'queued': True, 'task_id': str(async_result.id),
                'error': None}
    except Exception:  # noqa: BLE001 — no broker / enqueue failure → run inline
        logger.warning("%s: async enqueue failed, running inline", label, exc_info=True)

    account_ids = list(count_ids())
    if not account_ids:
        return {'success': True, 'queued': False, 'count': 0, 'error': None}

    if bounded:
        try:
            limit = int(TasafPaymentConfig.batch_inline_fallback_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit and len(account_ids) > limit:
            logger.error("%s: %d accounts exceeds the inline limit of %d and the queue "
                         "is unreachable", label, len(account_ids), limit)
            return {
                'success': False,
                'error': _("tasaf_payment.error.queue_unavailable_inline_limit"),
            }

    result = inline(account_ids)
    result['queued'] = False
    return result


def location_descendants_q(location_id, base='group_beneficiary__group__location'):
    """Q matching accounts whose household sits at *location_id* or anywhere below it.

    openIMIS locations are a fixed four-level hierarchy (Region/District/Ward/Village),
    so the parent chain is walked explicitly rather than recursively -- the same shape
    payroll uses for its beneficiary location filter. A PAA is a district, which is
    level two, so the deeper levels are what this actually reaches.
    """
    return (
        Q(**{f'{base}_id': location_id})
        | Q(**{f'{base}__parent_id': location_id})
        | Q(**{f'{base}__parent__parent_id': location_id})
        | Q(**{f'{base}__parent__parent__parent_id': location_id})
    )


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

    # Only these may be sent. PENDING_MUSE is already with MUSE, so a retried chunk or a second
    # click cannot send it twice; VERIFIED and MANUAL are finished or with an officer.
    DISPATCHABLE_STATUSES = (VerificationStatus.PENDING, VerificationStatus.FAILED)

    @check_authentication
    def dispatch(self, account_ids: list) -> dict:
        """
        Mark accounts as PENDING_MUSE and publish verification requests to GovESB:
        one message per FSP, split at ``verification_batch_max_rows``.

        Returns: {'success': bool, 'count': int, 'batches': int, 'skipped': int, 'error': str|None}
        """
        try:
            with transaction.atomic():
                accounts = list(
                    PaymentAccount.objects.select_for_update(of=('self',)).filter(
                        id__in=account_ids,
                        is_deleted=False,
                        verification_status__in=self.DISPATCHABLE_STATUSES,
                    ).select_related('group_beneficiary__group')
                )
                skipped = len(set(account_ids)) - len(accounts)
                if not accounts:
                    return {'success': True, 'count': 0, 'batches': 0, 'skipped': skipped,
                            'error': None}

                for account in accounts:
                    account.verification_status = VerificationStatus.PENDING_MUSE
                    account.save(username=self.user.username)

            # Published outside the atomic block so the commit is visible to the consumer.
            messages = 0
            for payload in self.messages(accounts):
                self._publish_payload(payload)
                messages += 1

            logger.info(
                "MuseVerificationDispatchService.dispatch: %d account(s) in %d message(s), "
                "%d skipped as already sent or not sendable, user=%s",
                len(accounts), messages, skipped, self.user.username,
            )
            return {
                'success': True, 'count': len(accounts),
                'batches': messages, 'skipped': skipped, 'error': None,
            }

        except Exception as exc:
            logger.exception("MuseVerificationDispatchService.dispatch failed")
            return output_exception(
                model_name="PaymentAccount",
                method="dispatch_verification",
                exception=exc,
            )

    @staticmethod
    def recipient_name(account) -> str:
        """The household representative's name -- what MUSE matches the account against.

        NOT the household head: measured on real data, the two differ in 37% of
        households, so using the head would fail verification for a third of the
        caseload and look like an FSP mismatch rather than our error.

        NOT PaymentAccount.account_name either -- that currently holds the HHID
        (the questionnaire never collected account details), so it is unusable as a name.
        """
        from individual.models import GroupIndividual

        group = getattr(getattr(account, 'group_beneficiary', None), 'group', None)
        if not group:
            return ''

        primary = (
            GroupIndividual.objects
            .filter(group=group, is_deleted=False, recipient_type='PRIMARY')
            .select_related('individual').first()
        )
        if primary and primary.individual:
            name = f"{primary.individual.first_name or ''} {primary.individual.last_name or ''}"
            if name.strip():
                return name.strip()
        return str((group.json_ext or {}).get('primary_recipient') or '').strip()

    def _batch_rows(self, accounts):
        """Group accounts into one batch per FSP, shaped the way MUSE expects.

        The key differs by channel -- mobile sends `name`, bank sends `account_name` --
        so the row is built per fsp_type rather than one shape for both.
        """
        from tasaf_payment.charges import resolve_fsp_code

        batches = {}
        for account in accounts:
            code = resolve_fsp_code(account.fsp_name) or 'UNMAPPED'
            batch = batches.setdefault(code, {
                'fsp_code': code,
                'fsp_name': account.fsp_name,
                'fsp_type': account.fsp_type,
                'rows': [],
            })
            group = getattr(getattr(account, 'group_beneficiary', None), 'group', None)
            row = {
                'account_number': account.account_number,
                # hhid is our reconciliation key; MUSE matches on number + name only.
                'hhid': getattr(group, 'code', None),
                'account_uuid': str(account.uuid),
            }
            name = self.recipient_name(account)
            if account.fsp_type == 'MOBILE':
                row['name'] = name
            else:
                row['account_name'] = name
            batch['rows'].append(row)
        return batches

    def messages(self, accounts):
        """The exact GovESB payloads a dispatch publishes: one FSP each, split at
        ``verification_batch_max_rows``. ``preview`` uses this too, so what it shows is
        what is sent."""
        from tasaf_payment.apps import TasafPaymentConfig

        size = int(TasafPaymentConfig.verification_batch_max_rows or 0) or max(len(accounts), 1)
        for batch in self._batch_rows(accounts).values():
            rows = batch['rows']
            for start in range(0, len(rows), size):
                part = rows[start:start + size]
                yield {
                    'fsp_code': batch['fsp_code'],
                    'fsp_name': batch['fsp_name'],
                    'fsp_type': batch['fsp_type'],
                    'count': len(part),
                    'requested_by': getattr(self.user, 'username', None),
                    'rows': part,
                }

    def _publish_payload(self, payload) -> None:
        """One GovESB message. Fail-soft, as elsewhere."""
        _govesb_publish(
            self.GOVESB_TOPIC_VERIFICATION_REQUEST,
            payload,
            user_id=getattr(self.user, 'username', None),
            context=f"verification fsp={payload['fsp_code']} rows={payload['count']}",
        )

    def preview(self, account_ids: list, sample_rows: int = 20) -> dict:
        """What a dispatch of these accounts would publish, without publishing or changing
        anything. Rows are trimmed to ``sample_rows`` per message for display; ``count`` keeps
        the real number."""
        from django.conf import settings
        from tasaf_payment.apps import TasafPaymentConfig
        try:
            from coremis_app_integration.govesb import GovESBProducer, govesb_enabled
            enabled = govesb_enabled()
            api_code = GovESBProducer._resolve_api_code(self.GOVESB_TOPIC_VERIFICATION_REQUEST)
        except ImportError:
            enabled, api_code = False, None

        accounts = list(PaymentAccount.objects.filter(
            id__in=account_ids, is_deleted=False,
            verification_status__in=self.DISPATCHABLE_STATUSES,
        ).select_related('group_beneficiary__group'))
        messages = []
        for payload in self.messages(accounts):
            shown = {**payload, 'rows': payload['rows'][:sample_rows]}
            messages.append({
                'fsp_code': payload['fsp_code'],
                'count': payload['count'],
                'rows_shown': len(shown['rows']),
                # The ESB client wraps every payload as {"requestdata": ...} and signs it.
                'body': {'requestdata': shown},
            })
        return {
            'topic': self.GOVESB_TOPIC_VERIFICATION_REQUEST,
            'api_code': api_code,
            'govesb_enabled': enabled,
            'esb_url': ((getattr(settings, 'ESB', None) or {}).get('ENGINE_URL') if enabled else None),
            'request_type': 'PUSH',
            'accounts': len(accounts),
            'max_rows_per_message': int(TasafPaymentConfig.verification_batch_max_rows or 0),
            'messages': messages,
        }


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
                account.verification_status = new_status
                account.muse_verification_reference = muse_ref
                account.save()

                if v_type == MuseVerificationType.ACTIVE_CHECK:
                    account.active_check_status = (
                        ActiveCheckStatus.ACTIVE
                        if result_str == 'PASSED'
                        else ActiveCheckStatus.INACTIVE
                    )
                    account.save()

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
            ).select_related('group_beneficiary__group')

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
        Return a list of failure reason CODES for this account.
        Empty list means the account passes pre-audit.

        Codes, not prose: these are stored on json_ext and rendered in the Pre-audit
        tab's Reason column, so the wording belongs in the frontend translations
        (`tasafPayment.preAudit.reason.<CODE>`). Rows written before this change hold
        English sentences; the frontend falls back to showing those verbatim.
        """
        reasons = []
        if account.verification_status != VerificationStatus.VERIFIED:
            reasons.append('NOT_VERIFIED')
        if not account.is_primary:
            reasons.append('NOT_PRIMARY')
        if account.group_beneficiary is None:
            reasons.append('NO_BENEFICIARY')
        elif account.group_beneficiary.status != 'ACTIVE':
            reasons.append('BENEFICIARY_INACTIVE')
        if account.fsp_type == 'MOBILE':
            from tasaf_payment.msisdn import is_valid
            if not is_valid(account.account_number):
                reasons.append('INVALID_MOBILE_NUMBER')
        return reasons


class PaylistService:
    """
    Generate, approve, and submit paylists to MUSE via GovESB.

    One payroll can produce multiple paylists (e.g., one Bank + one MNO).
    The paylist is the TASAF-specific dispatch batch — not the raw payroll.

    generate():  Build Paylist + PaylistItems from verified + pre-audited accounts.
    approve():   Move paylist from PENDING_APPROVAL → APPROVED.
    submit():    Move paylist from APPROVED → SUBMITTED and publish to GovESB.

    """

    GOVESB_TOPIC_PAYMENT_SUBMIT = 'tasaf.payment.submit'
    GOVESB_TOPIC_PAYMENT_SUBMIT_BY_DESTINATION = {
        'MUSE': 'tasaf.payment.submit',
        'GEPG': 'tasaf.payment.submit.gepg',
    }

    def __init__(self, user):
        self.user = user

    ALLOWED_PAYROLL_STATUSES = ('APPROVE_FOR_PAYMENT',)

    @staticmethod
    def _resolve_destination(destination):
        """Normalise a destination to a PaymentDestination value, or None if unknown.

        Absent/blank means MUSE, so pre-GePG callers keep working unchanged.
        """
        if not destination:
            return PaymentDestination.MUSE.value
        value = str(destination).strip().upper()
        if value in PaymentDestination.values:
            return value
        return None

    def _payroll_status_error(self, payroll_id):
        """Return an error string if the payroll may not be disbursed, else None."""
        from payroll.models import Payroll

        status = (
            Payroll.objects.filter(id=payroll_id, is_deleted=False)
            .values_list('status', flat=True)
            .first()
        )
        if status is None:
            return _("tasaf_payment.error.payroll_not_found")
        if status not in self.ALLOWED_PAYROLL_STATUSES:
            logger.info("PaylistService: payroll %s is %s, not approved for payment",
                        payroll_id, status)
            return _("tasaf_payment.error.payroll_not_approved")
        return None

    @check_authentication
    def generate(
        self,
        payroll_id,            # UUID — Payroll PK
        batch_type: str,
        payment_cycle_id=None,  # UUID — PaymentCycle PK
        destination: str = None,  # MUSE / GEPG — defaults to MUSE
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

            status_error = self._payroll_status_error(payroll_id)
            if status_error:
                return {'success': False, 'error': status_error}

            destination = self._resolve_destination(destination)
            if destination is None:
                return {'success': False, 'error': _("tasaf_payment.error.unknown_destination")}

            accepted = BenefitConsumption.objects.filter(
                id__in=PayrollBenefitConsumption.objects.filter(
                    payroll_id=payroll_id, is_deleted=False,
                ).values_list('benefit_id', flat=True),
                status=BenefitConsumptionStatus.ACCEPTED,
                is_deleted=False,
            )
            if not accepted.exists():
                return {'success': False, 'error': _("tasaf_payment.error.no_accepted_benefits")}
            benefit_count = accepted.exclude(id__in=benefits_on_a_paylist()).count()
            if benefit_count == 0:
                return {'success': False, 'error': _("tasaf_payment.error.benefits_already_on_paylists")}

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
                        destination,
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

            return self._generate_sync(payroll_id, batch_type, payment_cycle_id, destination)

        except Exception as exc:
            logger.exception("PaylistService.generate failed")
            return output_exception(model_name="Paylist", method="generate", exception=exc)

    def _generate_sync(
        self,
        payroll_id,
        batch_type: str,
        payment_cycle_id=None,
        destination: str = None,
    ) -> dict:
        """
        Build the Paylists + items (the heavy worker behind :meth:`generate`).

        Only accounts with verification_status=VERIFIED, pre_audit_status=PASSED,
        is_primary=True, is_deleted=False are included.

        Batching rule (MUSE): a paylist is BANK or MNO, never both, and the eligible accounts
        are split into Paylists of at most ``TasafPaymentConfig.paylist_max_batch_size``
        transactions (default 50000; 0/None = no cap).
        Sibling batches from one run share a ``batch_group`` UUID and carry
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
                Payroll,
                PayrollBenefitConsumption,
            )
            from tasaf_payment.apps import TasafPaymentConfig

            status_error = self._payroll_status_error(payroll_id)
            if status_error:
                return {'success': False, 'error': status_error}

            destination = self._resolve_destination(destination)
            if destination is None:
                return {'success': False, 'error': _("tasaf_payment.error.unknown_destination")}

            try:
                max_size = int(TasafPaymentConfig.paylist_max_batch_size or 0)
            except (TypeError, ValueError):
                max_size = 0
            if max_size < 0:
                max_size = 0

            user_pk = getattr(self.user, 'id', None)

            fsp_targets = {'BANK': [('BANK', 'BANK')], 'MNO': [('MOBILE', 'MNO')]}.get(batch_type)
            if not fsp_targets:
                return {'success': False, 'error': _("tasaf_payment.validation.invalid_batch_type")}

            benefit_ids = PayrollBenefitConsumption.objects.filter(
                payroll_id=payroll_id,
                is_deleted=False,
            ).values_list('benefit_id', flat=True)

            created = []

            with transaction.atomic():
                # One generation per payroll at a time, so two runs cannot both pass the check below.
                Payroll.objects.select_for_update().filter(id=payroll_id).first()
                accepted = BenefitConsumption.objects.filter(
                    id__in=benefit_ids,
                    status=BenefitConsumptionStatus.ACCEPTED,
                    is_deleted=False,
                )
                if not accepted.exists():
                    return {'success': False, 'error': _("tasaf_payment.error.no_accepted_benefits")}
                benefits = list(accepted.exclude(id__in=benefits_on_a_paylist()).select_related('individual'))
                skipped_on_paylist = accepted.count() - len(benefits)
                if not benefits:
                    return {'success': False, 'error': _("tasaf_payment.error.benefits_already_on_paylists")}

                for fsp_type, stored_type in fsp_targets:
                    pairs = payable_pairs(benefits, fsp_type)
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
                        paylist = Paylist(
                            payroll_id=payroll_id,
                            payment_cycle_id=payment_cycle_id,
                            batch_type=stored_type,
                            destination=destination,
                            status=PaylistStatus.PENDING_APPROVAL,
                            generated_at=now,
                            batch_group=group_id,
                            batch_sequence=seq,
                            batch_total=total,
                        )
                        paylist.save(user=self.user)
                        item_objs = [
                            self._build_paylist_item(paylist, benefit, account, now, user_pk)
                            for benefit, account in chunk
                        ]
                        PaylistItem.objects.bulk_create(item_objs, batch_size=2000)
                        self._start_paylist_approval(paylist)
                        created.append({
                            'paylist_uuid':   str(paylist.uuid),
                            'batch_type':     stored_type,
                            'batch_sequence': seq,
                            'batch_total':    total,
                            'item_count':     len(chunk),
                        })

                if not created:
                    return {'success': False, 'error': _("tasaf_payment.error.no_eligible_accounts")}

            total_items = sum(c['item_count'] for c in created)
            if skipped_on_paylist:
                logger.info("PaylistService.generate: payroll=%s skipped %d benefit(s) already on a paylist",
                            payroll_id, skipped_on_paylist)
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
                'paylist_uuid': created[0]['paylist_uuid'],
                'item_count': total_items,
                'skipped_on_paylist': skipped_on_paylist,
            }

        except Exception as exc:
            logger.exception("PaylistService.generate failed")
            return output_exception(
                model_name="Paylist",
                method="generate",
                exception=exc,
            )

    @check_authentication

    def preview_generation(self, payroll_id, batch_type, destination=None):
        """What generating this batch would do, without writing anything: who is included (count,
        amounts, per FSP), who is left out and why, and what will cause trouble later."""
        from payroll.models import BenefitConsumption, BenefitConsumptionStatus, PayrollBenefitConsumption
        from tasaf_payment.apps import TasafPaymentConfig
        from tasaf_payment.charges import ChargeError, gross_up, resolve_fsp_code
        from tasaf_payment.models import FspProfile

        error = self._payroll_status_error(payroll_id)
        if error:
            return {'success': False, 'error': error}
        destination = self._resolve_destination(destination)
        fsp_type = GENERATION_FSP_TYPES.get(batch_type)
        if destination is None or fsp_type is None:
            return {'success': False, 'error': _("tasaf_payment.validation.invalid_batch_type")}

        accepted = BenefitConsumption.objects.filter(
            id__in=PayrollBenefitConsumption.objects.filter(payroll_id=payroll_id, is_deleted=False)
            .values('benefit_id'),
            status=BenefitConsumptionStatus.ACCEPTED, is_deleted=False,
        )
        accepted_count = accepted.count()
        benefits = list(accepted.exclude(id__in=benefits_on_a_paylist()))
        pairs = payable_pairs(benefits, fsp_type)
        paired = {b.id for b, _ in pairs}
        missing = [b for b in benefits if b.id not in paired]
        reasons = exclusion_reasons([b.individual_id for b in missing], fsp_type)

        excluded = {'ALREADY_ON_PAYLIST': accepted_count - len(benefits)}
        for b in missing:
            reason = reasons.get(b.individual_id, 'NO_ACCOUNT')
            excluded[reason] = excluded.get(reason, 0) + 1

        charges_on = bool(TasafPaymentConfig.apply_withdrawal_charges)
        profiles = {p.fsp_code: p for p in FspProfile.objects.filter(is_deleted=False)}
        per_fsp, codes, no_band = {}, {}, {}
        net_total = gross_total = 0
        for benefit, account in pairs:
            name = account.fsp_name
            if name not in codes:
                try:
                    codes[name] = resolve_fsp_code(name)
                except ChargeError:
                    codes[name] = name or '—'
            code = codes[name]
            net, gross = benefit.amount or 0, benefit.amount or 0
            if charges_on:
                try:
                    net, _charge, gross = gross_up(name, benefit.amount)
                except ChargeError:
                    no_band[code] = no_band.get(code, 0) + 1
            row = per_fsp.setdefault(code, {'fsp_code': code, 'payments': 0, 'gross': 0})
            row['payments'] += 1
            row['gross'] += gross
            net_total += net
            gross_total += gross

        warnings = []
        if destination == PaymentDestination.MUSE:
            for code, row in per_fsp.items():
                p = profiles.get(code)
                if not (p and p.bank_name and p.bic and p.fsp_type):
                    warnings.append({'code': 'FSP_MISSING_MUSE_DETAILS', 'fsp_code': code, 'payments': row['payments'],
                                     'awaiting_muse': fsp_type == 'MOBILE'})
        for code, count in no_band.items():
            warnings.append({'code': 'NO_CHARGE_BAND', 'fsp_code': code, 'payments': count})

        return {
            'success': True,
            'accepted': accepted_count,
            'included': len(pairs),
            'net_total': float(net_total),
            'gross_total': float(gross_total),
            'charges_applied': charges_on,
            'per_fsp': sorted(({**r, 'gross': float(r['gross'])} for r in per_fsp.values()),
                              key=lambda r: -r['payments']),
            'excluded': {k: v for k, v in excluded.items() if v},
            'warnings': warnings,
        }

    def _build_paylist_item(self, paylist, benefit, account, now, user_pk):
        """One line. When charges are enabled the transfer is grossed up so the beneficiary
        withdraws the entitlement in full; net/charge/gross are all stored because
        reconciliation compares MUSE's gross against the benefit's net.

        A missing tariff band is NOT treated as a zero charge -- that would underpay by the
        fee, the exact failure this feature prevents -- so the line is left un-grossed and
        flagged for the operator.
        """
        from tasaf_payment.apps import TasafPaymentConfig
        net = benefit.amount
        charge = None
        gross = net
        note = None
        if TasafPaymentConfig.apply_withdrawal_charges:
            from tasaf_payment.charges import ChargeError, gross_up
            try:
                net, charge, gross = gross_up(account.fsp_name, benefit.amount)
            except ChargeError as exc:
                note = {'charge_error': exc.code, 'detail': exc.message}
        return PaylistItem(
            id=uuid.uuid4(),
            paylist=paylist,
            payment_account=account,
            benefit_consumption=benefit,
            amount=gross,
            net_amount=net,
            charge_amount=charge,
            status=PaylistItemStatus.PENDING,
            is_deleted=False,
            version=1,
            date_created=now,
            date_updated=now,
            user_created_id=user_pk,
            user_updated_id=user_pk,
            json_ext=note or {},
        )

    def _start_paylist_approval(self, paylist):
        """Kick off the two-level PAYMENT_APPROVAL sign-off in the generic Approval Engine.
        Best-effort — never breaks paylist generation."""
        try:
            from approval.services import ApprovalService
        except Exception as exc:
            logger.warning("tasaf_payment: approval engine unavailable (%s)", exc)
            return
        try:
            summary = {
                'batch_type': paylist.batch_type,
                'batch_sequence': paylist.batch_sequence,
                'batch_total': paylist.batch_total,
                'payroll_id': str(paylist.payroll_id) if paylist.payroll_id else None,
            }
            res = ApprovalService(self.user).request_approval(paylist, 'PAYMENT_APPROVAL', summary=summary)
            if not res.get('success'):
                logger.warning("tasaf_payment: paylist approval start failed: %s", res)
        except Exception as exc:
            logger.warning("tasaf_payment: paylist approval start error (%s)", exc)

    def _engine_request_for(self, paylist):
        """The open (PENDING) engine ApprovalRequest for this paylist, if any."""
        try:
            from approval.models import ApprovalRequest, RequestStatus as AStatus
            return ApprovalRequest.objects.filter(
                object_id=str(paylist.id), status=AStatus.PENDING, is_deleted=False,
            ).order_by('-date_created').first()
        except Exception:
            return None

    def approve(self, paylist_uuid: str) -> dict:
        """Approve the paylist's CURRENT step in the two-level Approval Engine sign-off.

        The paylist only reaches APPROVED once BOTH steps are signed by two DIFFERENT approvers
        (the engine's finalize adapter flips the status). Falls back to a direct transition for
        in-flight paylists that predate the engine (no ApprovalRequest)."""
        try:
            paylist = Paylist.objects.get(uuid=paylist_uuid, is_deleted=False)
            if paylist.status != PaylistStatus.PENDING_APPROVAL:
                logger.info("PaylistService.approve: paylist %s is %s", paylist_uuid, paylist.status)
                return {'success': False,
                        'error': _("tasaf_payment.error.paylist_not_pending_approval")}
            refusal = _wrong_type_refusal(paylist)
            if refusal:
                return refusal

            appr = self._engine_request_for(paylist)
            if appr:
                from approval.services import ApprovalService
                step = appr.steps.filter(order=appr.current_step_order, is_deleted=False).first()
                res = ApprovalService(self.user).approve(str(appr.id), str(step.id))
                if not res.get('success'):
                    return {'success': False,
                            'error': (res.get('message') or res.get('detail')
                                      or _("tasaf_payment.error.approval_failed"))}
                paylist.refresh_from_db()  # adapter sets APPROVED once the final step is signed
                logger.info("PaylistService.approve: paylist=%s step advanced (status=%s, user=%s)",
                            paylist_uuid, paylist.status, self.user.username)
                return {'success': True, 'error': None, 'status': paylist.status}

            paylist.status = PaylistStatus.APPROVED
            paylist.approved_at = datetime.now(tz=timezone.utc)
            paylist.save()
            logger.info("PaylistService.approve: paylist=%s (legacy direct, user=%s)",
                        paylist_uuid, self.user.username)
            return {'success': True, 'error': None, 'status': paylist.status}

        except Paylist.DoesNotExist:
            return {'success': False, 'error': _("tasaf_payment.error.paylist_not_found")}
        except Exception as exc:
            logger.exception("PaylistService.approve failed")
            return output_exception(model_name="Paylist", method="approve", exception=exc)

    @check_authentication
    def submit(self, paylist_uuid: str) -> dict:
        """APPROVED → SUBMITTED. A MUSE paylist becomes SUBMITTED only once GovESB accepts
        its message (or in RECORD_ONLY mode); otherwise it stays APPROVED."""
        try:
            paylist = Paylist.objects.get(uuid=paylist_uuid, is_deleted=False)
            if paylist.status not in (PaylistStatus.APPROVED, PaylistStatus.REJECTED):
                logger.info("PaylistService.submit: paylist %s is %s", paylist_uuid, paylist.status)
                return {'success': False, 'error': _("tasaf_payment.error.paylist_not_approved")}
            refusal = _wrong_type_refusal(paylist)
            if refusal:
                return refusal
            duplicates = paid_elsewhere(paylist).count()
            if duplicates:
                logger.warning("PaylistService.submit: paylist %s refused, %d benefit(s) paid elsewhere",
                               paylist_uuid, duplicates)
                return {'success': False, 'outcome': 'REFUSED',
                        'error': f'{duplicates} payment(s) on this paylist are already paid, or being paid, '
                                 f'on another paylist; submitting would pay them twice'}
            if paylist.status == PaylistStatus.REJECTED:
                _start_new_muse_attempt(paylist, self.user)

            if paylist.destination == PaymentDestination.MUSE:
                from tasaf_payment.muse_sender import MuseSender
                result = MuseSender(self.user).send(paylist, self.GOVESB_TOPIC_PAYMENT_SUBMIT)
                if not result.submitted:
                    logger.info("PaylistService.submit: paylist=%s not submitted (%s): %s",
                                paylist_uuid, result.outcome, result.message)
                    return {'success': False, 'error': result.message, 'outcome': result.outcome}
            else:
                self._publish_paylist(paylist)

            paylist.status = PaylistStatus.SUBMITTED
            paylist.submitted_at = datetime.now(tz=timezone.utc)
            paylist.save(user=self.user)

            logger.info("PaylistService.submit: paylist=%s (user=%s)", paylist_uuid, self.user.username)
            return {'success': True, 'error': None}

        except Paylist.DoesNotExist:
            return {'success': False, 'error': _("tasaf_payment.error.paylist_not_found")}
        except Exception as exc:
            logger.exception("PaylistService.submit failed")
            return output_exception(model_name="Paylist", method="submit", exception=exc)

    def _publish_paylist(self, paylist: Paylist) -> None:
        """GePG only: the flat payload over GovESB, fail-soft. MUSE paylists go through
        ``muse_sender.MuseSender``."""
        items = list(paylist.items.select_related('payment_account').all())
        payload = {
            'paylist_uuid':   str(paylist.uuid),
            'destination':    paylist.destination,
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
        destination = paylist.destination or PaymentDestination.MUSE.value
        topic = self.GOVESB_TOPIC_PAYMENT_SUBMIT_BY_DESTINATION.get(
            destination, self.GOVESB_TOPIC_PAYMENT_SUBMIT,
        )
        _govesb_publish(
            topic,
            payload,
            user_id=getattr(self.user, 'username', None),
            context=f"paylist={paylist.uuid} dest={destination} items={len(items)}",
        )


GENERATION_FSP_TYPES = {'BANK': 'BANK', 'MNO': 'MOBILE'}


def eligible_accounts(individual_ids, fsp_type):
    """{individual_id: PaymentAccount} — the account a benefit may be paid to: the household's
    primary account of this channel, VERIFIED, pre-audit PASSED, and for mobile a valid number.
    The one rule, shared by generation and its preview."""
    qs = PaymentAccount.objects.filter(
        group_beneficiary__group__groupindividuals__individual_id__in=individual_ids,
        group_beneficiary__group__groupindividuals__is_deleted=False,
        verification_status=VerificationStatus.VERIFIED,
        pre_audit_status=PreAuditStatus.PASSED,
        is_primary=True,
        is_deleted=False,
        fsp_type=fsp_type,
    )
    if fsp_type == 'MOBILE':
        from tasaf_payment.msisdn import MSISDN_DB_PATTERN
        qs = qs.filter(account_number__regex=MSISDN_DB_PATTERN)
    rows = list(qs.values_list('id', 'group_beneficiary__group__groupindividuals__individual_id'))
    accounts = PaymentAccount.objects.select_related('group_beneficiary__group').in_bulk({a for a, _ in rows})
    return {individual: accounts[account] for account, individual in rows}


def payable_pairs(benefits, fsp_type):
    """[(benefit, account)] in a stable order (account number, then benefit)."""
    account_map = eligible_accounts([b.individual_id for b in benefits], fsp_type)
    pairs = [(b, account_map[b.individual_id]) for b in benefits if b.individual_id in account_map]
    pairs.sort(key=lambda p: ((p[1].account_number or ''), str(p[0].id)))
    return pairs


def exclusion_reasons(individual_ids, fsp_type):
    """Why each individual has no payable account of this channel: the stage it is closest to."""
    rows = PaymentAccount.objects.filter(
        group_beneficiary__group__groupindividuals__individual_id__in=individual_ids,
        group_beneficiary__group__groupindividuals__is_deleted=False,
        is_primary=True, is_deleted=False,
    ).values_list('group_beneficiary__group__groupindividuals__individual_id', 'fsp_type',
                  'verification_status', 'pre_audit_status', 'account_number')
    by_individual = {}
    for individual, account_type, verification, pre_audit, number in rows:
        by_individual.setdefault(individual, []).append((account_type, verification, pre_audit, number))
    reasons = {}
    for individual in individual_ids:
        accounts = by_individual.get(individual, [])
        own = [a for a in accounts if a[0] == fsp_type]
        if not accounts:
            reasons[individual] = 'NO_ACCOUNT'
        elif not own:
            reasons[individual] = 'OTHER_CHANNEL'
        elif any(v == VerificationStatus.VERIFIED and p == PreAuditStatus.PASSED for _, v, p, _ in own):
            reasons[individual] = 'INVALID_MOBILE_NUMBER'
        elif any(v == VerificationStatus.VERIFIED for _, v, _, _ in own):
            reasons[individual] = 'PRE_AUDIT_NOT_PASSED'
        else:
            reasons[individual] = 'NOT_VERIFIED'
    return reasons


def benefits_on_a_paylist():
    """Benefit ids already on a live paylist. A benefit is paid from one paylist only; a batch
    rejected at approval releases its benefits."""
    return PaylistItem.objects.filter(
        is_deleted=False, paylist__is_deleted=False, benefit_consumption__isnull=False,
    ).exclude(paylist__status=PaylistStatus.REJECTED_AT_APPROVAL).values('benefit_consumption_id')


LOCKED_PAYMENT_FIELDS = ('account_number', 'fsp_name', 'fsp_type', 'account_name')
PAYLIST_FINISHED_STATUSES = (PaylistStatus.CLOSED, PaylistStatus.REJECTED_AT_APPROVAL)


def account_paid_at(account_id):
    item = (PaylistItem.objects.filter(payment_account_id=account_id, is_deleted=False,
                                       status=PaylistItemStatus.PROCESSED)
            .order_by('-settled_at', '-date_updated').first())
    return (item.settled_at or item.date_updated) if item else None


def account_payment_in_flight(account_id):
    return (PaylistItem.objects.filter(payment_account_id=account_id, is_deleted=False,
                                       status=PaylistItemStatus.PENDING, paylist__is_deleted=False)
            .exclude(paylist__status__in=PAYLIST_FINISHED_STATUSES).exists())


def paid_elsewhere(paylist):
    """Items of *paylist* whose benefit is already paid (PROCESSED), or being paid (on a paylist
    still with MUSE), on another paylist. Submitting them would pay the benefit twice."""
    others = PaylistItem.objects.filter(
        is_deleted=False, paylist__is_deleted=False,
        benefit_consumption_id__in=paylist.items.filter(is_deleted=False).values('benefit_consumption_id'),
    ).exclude(paylist_id=paylist.id).filter(
        Q(status=PaylistItemStatus.PROCESSED) | Q(paylist__status__in=PAYLIST_IN_FLIGHT_STATUSES))
    return paylist.items.filter(is_deleted=False,
                                benefit_consumption_id__in=others.values('benefit_consumption_id'))


def wrong_type_items(paylist):
    """Items whose account type does not match the paylist's batch type (BANK vs MNO/MOBILE)."""
    fsp_type = GENERATION_FSP_TYPES.get(paylist.batch_type)
    return paylist.items.filter(is_deleted=False).exclude(payment_account__fsp_type=fsp_type)


def _wrong_type_refusal(paylist):
    count = wrong_type_items(paylist).count()
    if not count:
        return None
    logger.warning("Paylist %s refused: %d item(s) do not match batch type %s",
                   paylist.uuid, count, paylist.batch_type)
    return {'success': False, 'outcome': 'REFUSED',
            'error': f'{count} payment(s) on this {paylist.batch_type} paylist use an account of another '
                     f'type; a paylist must hold only {GENERATION_FSP_TYPES.get(paylist.batch_type)} accounts'}


def settle_item(item, settled_at=None, reference=None):
    """PENDING item -> PROCESSED (paid); closes the paylist once every item is terminal."""
    with transaction.atomic():
        item.status = PaylistItemStatus.PROCESSED
        item.settled_at = _parse_dt(settled_at) or datetime.now(tz=timezone.utc)
        if reference:
            item.muse_reference = reference
        item.save(user=_inbound_audit_user(item))
        _close_paylist_if_complete(item.paylist)


def unapply_item(item, reason_code=None, reason_description=None, reference=None):
    """PENDING item -> UNAPPLIED, with the reason kept as ReturnFeedback. Terminal too."""
    user = _inbound_audit_user(item)
    with transaction.atomic():
        ReturnFeedback(
            paylist_item=item,
            feedback_type=ReturnFeedbackType.UNAPPLIED,
            reason_code=reason_code,
            reason_description=reason_description,
        ).save(user=user)
        item.status = PaylistItemStatus.UNAPPLIED
        item.return_reason = reason_description
        if reference:
            item.muse_reference = reference
        item.save(user=user)
        _close_paylist_if_complete(item.paylist)


def _start_new_muse_attempt(paylist, user):
    """A batch MUSE rejected goes back as a new message: next attempt number, new msgId and
    timestamp. referenceNo stays — it is the same batch."""
    ext = dict(paylist.json_ext or {})
    ext['muse_attempt'] = int(ext.get('muse_attempt') or 1) + 1
    ext.pop('muse_created_at', None)
    paylist.json_ext = ext
    paylist.muse_msg_id = None
    paylist.save(user=user)


MUSE_BATCH_ORDER = {s: i for i, s in enumerate(PAYLIST_IN_FLIGHT_STATUSES)}


def apply_muse_batch_status(paylist, status, description=None, at=None, user=None) -> bool:
    """Record a batch-level reply from MUSE (RECEIVED, ACCEPTED, SENT_TO_BANK, REJECTED).
    Only moves forward; a repeat or an older reply is ignored. Returns True if applied."""
    if paylist.status not in PAYLIST_IN_FLIGHT_STATUSES:
        return False
    if status == PaylistStatus.REJECTED:
        if MUSE_BATCH_ORDER[paylist.status] > MUSE_BATCH_ORDER[PaylistStatus.RECEIVED]:
            return False
    elif status not in MUSE_BATCH_ORDER or MUSE_BATCH_ORDER[status] <= MUSE_BATCH_ORDER[paylist.status]:
        return False
    paylist.status = status
    paylist.muse_status_desc = description
    paylist.muse_status_at = at or datetime.now(tz=timezone.utc)
    paylist.save(user=user or _inbound_audit_user(paylist))
    logger.info("Paylist %s -> %s (%s)", paylist.uuid, status, description)
    return True


def _close_paylist_if_complete(paylist) -> bool:
    """In flight -> CLOSED once every item is terminal. Idempotent."""
    if paylist.status not in PAYLIST_IN_FLIGHT_STATUSES:
        return False
    outstanding = paylist.items.filter(is_deleted=False).exclude(
        status__in=PAYLIST_ITEM_TERMINAL_STATUSES,
    ).exists()
    if outstanding:
        return False
    paylist.status = PaylistStatus.CLOSED
    paylist.closed_at = datetime.now(tz=timezone.utc)
    paylist.save(user=_inbound_audit_user(paylist))
    logger.info("Paylist %s closed — all items terminal", paylist.uuid)
    _notify_payroll_if_all_closed(paylist)
    return True


def _notify_payroll_if_all_closed(paylist) -> bool:
    """Hand the payroll to payroll's reconciliation (``acknowledge_of_reponse_view``) once no
    sibling paylist is still in flight, so a multi-batch run raises one task. Batches that were
    never dispatched do not block it. Fail-soft: a failed hand-off never loses the settlement.
    """
    payroll_id = paylist.payroll_id
    if not payroll_id:
        return False

    siblings = Paylist.objects.filter(payroll_id=payroll_id, is_deleted=False)
    if siblings.filter(status__in=PAYLIST_IN_FLIGHT_STATUSES).exists():
        return False
    dispatched = siblings.filter(status=PaylistStatus.CLOSED)
    if not dispatched.exists():
        return False

    try:
        from payroll.models import Payroll
        from payroll.payments_registry import PaymentMethodStorage

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            return False
        strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
        if not strategy:
            logger.warning("Paylist %s closed but payroll %s has no strategy (%r)",
                           paylist.uuid, payroll_id, payroll.payment_method)
            return False

        items = PaylistItem.objects.filter(paylist__in=dispatched, is_deleted=False)
        settled = items.filter(status=PaylistItemStatus.PROCESSED)
        summary = {
            'source': 'tasaf_payment',
            'paylists': dispatched.count(),
            'items': items.count(),
            'settled': settled.count(),
            'failed': items.filter(
                status=PaylistItemStatus.UNAPPLIED,
            ).count(),
            'settled_amount': str(
                settled.aggregate(total=Sum('net_amount'))['total'] or 0,
            ),
            'closed_at': datetime.now(tz=timezone.utc).isoformat(),
        }

        strategy.acknowledge_of_reponse_view(
            payroll, summary, _inbound_audit_user(paylist), [],
        )
        logger.info("Payroll %s acknowledged: %s", payroll_id, summary)
        return True

    except Exception:  # noqa: BLE001 — never lose a settlement over the hand-off
        logger.exception("Payroll hand-off failed for paylist %s", paylist.uuid)
        return False


class PaymentSettlementService:
    """
    Records a **successful** payment confirmed by the gateway.

    This is the counterpart to :class:`ReturnFeedbackService`, which only ever records
    failures. Without this path nothing sets ``PaylistItem.status = PROCESSED``, so the
    system cannot distinguish "paid" from "sent, no news" — both sit at PENDING.

    Expected payload (one item)::

        {
            "paylist_item_uuid": "...",
            "muse_reference":    "...",     # gateway's own reference, optional
            "settled_at":        "ISO8601", # optional, defaults to now
        }

    Batch form: ``{"items": [ {...}, {...} ]}`` — see :meth:`handle_batch_settlement`.
    """

    def handle_settlement(self, payload: dict) -> dict:
        """Mark one paylist item as successfully paid."""
        item_uuid = payload.get('paylist_item_uuid')
        try:
            item = PaylistItem.objects.get(uuid=item_uuid, is_deleted=False)
        except PaylistItem.DoesNotExist:
            logger.error("PaymentSettlementService: unknown paylist_item_uuid=%s", item_uuid)
            return {'success': False, 'error': f'PaylistItem {item_uuid} not found'}

        if item.status == PaylistItemStatus.UNAPPLIED:
            return {
                'success': False,
                'error': f'Item {item_uuid} is {item.status}; settlement rejected',
            }

        if item.status == PaylistItemStatus.PROCESSED:
            return {'success': True, 'error': None, 'already_settled': True}

        try:
            settle_item(item, payload.get('settled_at'), payload.get('muse_reference'))
            logger.info("PaymentSettlementService: item=%s settled", item_uuid)
            return {'success': True, 'error': None}

        except Exception as exc:
            logger.exception("PaymentSettlementService.handle_settlement failed item=%s", item_uuid)
            return output_exception(
                model_name="PaylistItem", method="handle_settlement", exception=exc,
            )

    def handle_batch_settlement(self, payload: dict) -> dict:
        """
        Settle many items in one message — the shape a batch settlement file takes.

        Partial success is normal and is reported rather than raised: one unknown or
        already-returned item must not discard the rest of the batch.
        """
        items = payload.get('items') or []
        if not isinstance(items, list):
            return {'success': False, 'error': "'items' must be a list"}

        settled, rejected = 0, []
        for entry in items:
            result = self.handle_settlement(entry)
            if result.get('success'):
                settled += 1
            else:
                rejected.append({
                    'paylist_item_uuid': entry.get('paylist_item_uuid'),
                    'error': result.get('error'),
                })

        logger.info(
            "PaymentSettlementService: batch settled=%d rejected=%d",
            settled, len(rejected),
        )
        return {
            'success': True, 'error': None,
            'settled': settled, 'rejected_count': len(rejected), 'rejected': rejected,
        }


class ReturnFeedbackService:
    """
    Receives and stores unapplied feedback from MUSE via GovESB (a bank return included).

    Called by:
    - The stub REST endpoint (POST /api/tasaf_payment/muse/return_feedback/)
      for development/testing.
    - The GovESB consumer in coremis_app_integration (when available).

    Expected payload:
    {
        "paylist_item_uuid": "...",
        "feedback_type":     "UNAPPLIED",
        "reason_code":       "...",
        "reason_description": "...",
        "muse_reference":    "..."
    }
    """

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
            unapply_item(item, payload.get('reason_code'), payload.get('reason_description'),
                         payload.get('muse_reference'))

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


class VerificationService(MuseVerificationDispatchService):
    """Deprecated alias. Use MuseVerificationDispatchService."""

    def run_verification(self, account_ids: list) -> dict:
        return self.dispatch(account_ids)

    def approve_accounts(self, account_ids: list, approved: bool, review_notes: str = '') -> dict:
        return ManualApprovalService(self.user).approve_accounts(account_ids, approved, review_notes)


class BatchVerificationService:
    """Queue a filter-scoped MUSE verification dispatch.

    Not a deprecated alias despite its former docstring -- this is the live path behind
    the runBatchVerification mutation. MuseVerificationDispatchService is the per-account
    worker it fans out to.
    """

    def __init__(self, user):
        self.user = user

    def dispatch(self, filters: dict) -> dict:
        from tasaf_payment.tasks import _build_account_queryset, run_batch_verification_task
        try:
            return _queue_or_inline(
                task=run_batch_verification_task,
                task_args=(filters, self.user.id),
                inline=lambda ids: MuseVerificationDispatchService(self.user).dispatch(ids),
                count_ids=lambda: _build_account_queryset(filters).values_list('id', flat=True),
                label='BatchVerificationService.dispatch',
                # Inline dispatch publishes to GovESB, so it is capped.
                bounded=True,
            )
        except Exception as exc:
            logger.exception("BatchVerificationService.dispatch failed")
            return output_exception(
                model_name="PaymentAccount",
                method="dispatch_batch_verification",
                exception=exc,
            )


class BatchPreAuditService:
    """Queue a filter-scoped pre-audit run.

    Mirrors :class:`BatchVerificationService`: the caller passes filters and the task
    rebuilds the queryset worker-side, so a PAA-wide run covers every candidate rather
    than whatever a searcher page happened to have loaded.
    """

    def __init__(self, user):
        self.user = user

    def dispatch(self, filters: dict) -> dict:
        from tasaf_payment.tasks import _build_pre_audit_queryset, run_batch_pre_audit_task
        try:
            return _queue_or_inline(
                task=run_batch_pre_audit_task,
                task_args=(filters, self.user.id),
                inline=lambda ids: PreAuditService(self.user).run_pre_audit(ids),
                count_ids=lambda: _build_pre_audit_queryset(filters).values_list('id', flat=True),
                label='BatchPreAuditService.dispatch',
                bounded=False,
            )
        except Exception as exc:
            logger.exception("BatchPreAuditService.dispatch failed")
            return output_exception(
                model_name="PaymentAccount", method="dispatch_batch_pre_audit",
                exception=exc,
            )


# ─── Withdrawal-charge tariff maintenance (maker-checker) ─────────────────────

class WithdrawalChargeService(BaseService, UpdateCheckerLogicServiceMixin):
    """Maker-checker for tariff bands, mirroring PmtGlobalFormulaService.

    A tariff edit changes what every beneficiary is paid, so when
    ``charges_require_approval`` is on the mutation records a tasks_management Task via
    ``create_update_task`` instead of writing. A second user approving that task runs the
    real ``update`` below, through
    ``on_task_complete_service_handler(WithdrawalChargeService)`` bound in signals.
    """
    OBJECT_TYPE = WithdrawalCharge

    def __init__(self, user, validation_class=WithdrawalChargeValidation):
        super().__init__(user, validation_class)

    @register_service_signal('withdrawal_charge_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('withdrawal_charge_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('withdrawal_charge_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)
