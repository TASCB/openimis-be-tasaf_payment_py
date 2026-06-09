import graphene
from gettext import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from core.gql.gql_mutations.base_mutation import (
    BaseHistoryModelCreateMutationMixin,
    BaseHistoryModelUpdateMutationMixin,
    BaseHistoryModelDeleteMutationMixin,
    BaseMutation,
)
from core.schema import OpenIMISMutation
from tasaf_payment.apps import TasafPaymentConfig
from tasaf_payment.models import PaymentAccount, VerificationStatus


def _resolve_account_ids(uuids):
    """Convert list of UUIDs to DB IDs."""
    return list(
        PaymentAccount.objects.filter(
            uuid__in=uuids,
            is_deleted=False,
        ).values_list('id', flat=True)
    )


def _require_perms(user, perms):
    if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
        raise ValidationError(_("mutation.authentication_required"))


# ─── Input types ─────────────────────────────────────────────────────────────

class CreatePaymentAccountInputType(OpenIMISMutation.Input):
    group_beneficiary_id = graphene.UUID(required=True)
    account_number = graphene.String(required=True, max_length=50)
    account_name = graphene.String(required=False, max_length=255)
    fsp_type = graphene.String(required=True, max_length=20)
    fsp_name = graphene.String(required=True, max_length=100)
    is_primary = graphene.Boolean(required=False)
    json_ext = graphene.JSONString(required=False)


class UpdatePaymentAccountInputType(CreatePaymentAccountInputType):
    id = graphene.UUID(required=True)


class DeletePaymentAccountInputType(OpenIMISMutation.Input):
    ids = graphene.List(graphene.UUID, required=True)


class RunVerificationInputType(OpenIMISMutation.Input):
    account_uuids = graphene.List(graphene.UUID, required=True)


class ApprovePaymentAccountsInputType(OpenIMISMutation.Input):
    account_uuids = graphene.List(graphene.UUID, required=True)
    approved = graphene.Boolean(required=True)
    review_notes = graphene.String(required=False)


class RunBatchVerificationInputType(OpenIMISMutation.Input):
    benefit_plan_id = graphene.UUID(required=False)
    fsp_type        = graphene.String(required=False)
    rerun           = graphene.Boolean(required=False)
    account_uuids   = graphene.List(graphene.UUID, required=False)


class ResubmitFailedAccountsInputType(OpenIMISMutation.Input):
    account_uuids = graphene.List(graphene.UUID, required=True)


class RunPreAuditInputType(OpenIMISMutation.Input):
    account_uuids = graphene.List(graphene.UUID, required=True)


class GeneratePaylistInputType(OpenIMISMutation.Input):
    # Payroll / PaymentCycle are HistoryModels with UUID primary keys.
    payroll_id       = graphene.UUID(required=True)
    batch_type       = graphene.String(required=True)   # BANK / MNO / MIXED
    payment_cycle_id = graphene.UUID(required=False)
    location_id      = graphene.Int(required=False)      # Location uses a legacy integer PK


class ApprovePaylistInputType(OpenIMISMutation.Input):
    paylist_uuid = graphene.UUID(required=True)


class SubmitPaylistInputType(OpenIMISMutation.Input):
    paylist_uuid = graphene.UUID(required=True)


class RouteToCorrection(OpenIMISMutation.Input):
    """Send FAILED accounts to tasks_management for case management correction."""
    account_uuids = graphene.List(graphene.UUID, required=True)
    notes = graphene.String(required=False)


# ─── PaymentAccount CRUD ─────────────────────────────────────────────────────

class CreatePaymentAccountMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreatePaymentAccountMutation"
    _mutation_module = TasafPaymentConfig.name
    _model = PaymentAccount

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_payment_account_create_perms)

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaymentAccountService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        PaymentAccountService(user).create(data)

    class Input(CreatePaymentAccountInputType):
        pass


class UpdatePaymentAccountMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdatePaymentAccountMutation"
    _mutation_module = TasafPaymentConfig.name
    _model = PaymentAccount

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_payment_account_update_perms)

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaymentAccountService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        PaymentAccountService(user).update(data)

    class Input(UpdatePaymentAccountInputType):
        pass


class DeletePaymentAccountMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeletePaymentAccountMutation"
    _mutation_module = TasafPaymentConfig.name
    _model = PaymentAccount

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_payment_account_delete_perms)

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaymentAccountService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        service = PaymentAccountService(user)
        for account_id in data.get('ids', []):
            service.delete({'id': account_id})

    class Input(DeletePaymentAccountInputType):
        pass


# ─── Verification mutations ───────────────────────────────────────────────────

class RunVerificationMutation(BaseMutation):
    """Dispatch selected accounts to MUSE for verification via GovESB."""
    _mutation_class = "RunVerificationMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_run_verification_perms)
        if not data.get('account_uuids'):
            raise ValidationError(_("tasaf_payment.validation.no_accounts_selected"))

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import MuseVerificationDispatchService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        account_ids = _resolve_account_ids(data.get('account_uuids', []))
        result = MuseVerificationDispatchService(user).dispatch(account_ids)
        if not result.get('success'):
            raise Exception(result.get('error', 'Verification dispatch failed'))

    class Input(RunVerificationInputType):
        pass


class ApprovePaymentAccountsMutation(BaseMutation):
    """Approve or reject MANUAL-status accounts after human review."""
    _mutation_class = "ApprovePaymentAccountsMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_approve_account_perms)
        if not data.get('account_uuids'):
            raise ValidationError(_("tasaf_payment.validation.no_accounts_selected"))

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import ManualApprovalService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        account_ids = _resolve_account_ids(data.get('account_uuids', []))
        result = ManualApprovalService(user).approve_accounts(
            account_ids,
            data.get('approved', False),
            data.get('review_notes', ''),
        )
        if not result.get('success'):
            raise Exception(result.get('error', 'Approval failed'))

    class Input(ApprovePaymentAccountsInputType):
        pass


class RunBatchVerificationMutation(BaseMutation):
    """Dispatch a large-scale batch verification job to Celery."""
    _mutation_class = "RunBatchVerificationMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_run_verification_perms)
        has_filter = any([
            data.get('benefit_plan_id'),
            data.get('fsp_type'),
            data.get('account_uuids'),
        ])
        if not has_filter:
            raise ValidationError(
                _("tasaf_payment.validation.batch_verification_requires_filter")
            )

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import BatchVerificationService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        filters = {}
        if data.get('benefit_plan_id'):
            filters['benefit_plan_id'] = str(data['benefit_plan_id'])
        if data.get('fsp_type'):
            filters['fsp_type'] = data['fsp_type']
        if data.get('rerun'):
            filters['rerun'] = bool(data['rerun'])
        if data.get('account_uuids'):
            filters['account_uuids'] = [str(u) for u in data['account_uuids']]
        result = BatchVerificationService(user).dispatch(filters)
        if not result.get('success'):
            raise Exception(result.get('error', 'Batch verification dispatch failed'))

    class Input(RunBatchVerificationInputType):
        pass


class ResubmitFailedAccountsMutation(BaseMutation):
    """Reset FAILED accounts to PENDING and re-dispatch to MUSE."""
    _mutation_class = "ResubmitFailedAccountsMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_resubmit_failed_perms)
        if not data.get('account_uuids'):
            raise ValidationError(_("tasaf_payment.validation.no_accounts_selected"))

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.tasks import resubmit_failed_accounts_task
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        account_ids = list(
            PaymentAccount.objects.filter(
                uuid__in=data.get('account_uuids', []),
                verification_status=VerificationStatus.FAILED,
                is_deleted=False,
            ).values_list('id', flat=True)
        )
        if account_ids:
            resubmit_failed_accounts_task.delay(account_ids, user.id)

    class Input(ResubmitFailedAccountsInputType):
        pass


# ─── Pre-audit mutation ───────────────────────────────────────────────────────

class RunPreAuditMutation(BaseMutation):
    """Run pre-audit checks on selected verified accounts."""
    _mutation_class = "RunPreAuditMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_run_pre_audit_perms)
        if not data.get('account_uuids'):
            raise ValidationError(_("tasaf_payment.validation.no_accounts_selected"))

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PreAuditService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        account_ids = _resolve_account_ids(data.get('account_uuids', []))
        result = PreAuditService(user).run_pre_audit(account_ids)
        if not result.get('success'):
            raise Exception(result.get('error', 'Pre-audit failed'))

    class Input(RunPreAuditInputType):
        pass


# ─── Paylist mutations ────────────────────────────────────────────────────────

class GeneratePaylistMutation(BaseMutation):
    """Generate a Paylist from verified + pre-audited accounts in a payroll."""
    _mutation_class = "GeneratePaylistMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_generate_paylist_perms)
        if not data.get('payroll_id'):
            raise ValidationError(_("tasaf_payment.validation.payroll_id_required"))
        if data.get('batch_type') not in ('BANK', 'MNO', 'MIXED'):
            raise ValidationError(_("tasaf_payment.validation.invalid_batch_type"))

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaylistService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        result = PaylistService(user).generate(
            payroll_id=data['payroll_id'],
            batch_type=data['batch_type'],
            payment_cycle_id=data.get('payment_cycle_id'),
            location_id=data.get('location_id'),
        )
        if not result.get('success'):
            raise Exception(result.get('error', 'Paylist generation failed'))

    class Input(GeneratePaylistInputType):
        pass


class ApprovePaylistMutation(BaseMutation):
    """Move a paylist from PENDING_APPROVAL to APPROVED."""
    _mutation_class = "ApprovePaylistMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_approve_paylist_perms)

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaylistService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        result = PaylistService(user).approve(str(data['paylist_uuid']))
        if not result.get('success'):
            raise Exception(result.get('error', 'Paylist approval failed'))

    class Input(ApprovePaylistInputType):
        pass


class SubmitPaylistMutation(BaseMutation):
    """Move an APPROVED paylist to SUBMITTED and publish to GovESB."""
    _mutation_class = "SubmitPaylistMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_submit_paylist_perms)

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaylistService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        result = PaylistService(user).submit(str(data['paylist_uuid']))
        if not result.get('success'):
            raise Exception(result.get('error', 'Paylist submission failed'))

    class Input(SubmitPaylistInputType):
        pass


class RouteToCorrectionMutation(BaseMutation):
    """
    Route FAILED verification accounts to Case Management for correction.

    Creates a tasks_management Task for each account. Scaffold only —
    full Case Management integration is a future development item.
    """
    _mutation_class = "RouteToCorrectionMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_approve_account_perms)
        if not data.get('account_uuids'):
            raise ValidationError(_("tasaf_payment.validation.no_accounts_selected"))

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        account_uuids = data.get('account_uuids', [])
        notes = data.get('notes', '')
        accounts = PaymentAccount.objects.filter(
            uuid__in=account_uuids,
            verification_status=VerificationStatus.FAILED,
            is_deleted=False,
        )
        # Scaffold: for now this only logs the route-to-correction intent.
        # TODO: create a tasks_management task to drive the Case Management
        # correction workflow (not yet implemented).
        import logging
        logger = logging.getLogger(__name__)
        for account in accounts:
            logger.info(
                "[CASE_MGMT SCAFFOLD] Route account %s to correction. Notes: %s",
                account.uuid, notes,
            )

    class Input(RouteToCorrection):
        pass
