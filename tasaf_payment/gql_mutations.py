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
from tasaf_payment.models import (
    PaymentAccount, WithdrawalCharge, FspMapping, PaymentDestination, BatchType,
)


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
    # The Verification list's own filters, so "Verify all matching" sends exactly that list.
    fsp_name_icontains = graphene.String(required=False)
    account_number_icontains = graphene.String(required=False)
    location_id     = graphene.Int(required=False)
    rerun           = graphene.Boolean(required=False)
    account_uuids   = graphene.List(graphene.UUID, required=False)


class RunPreAuditInputType(OpenIMISMutation.Input):
    account_uuids = graphene.List(graphene.UUID, required=True)


class RunBatchPreAuditInputType(OpenIMISMutation.Input):
    benefit_plan_id = graphene.UUID(required=False)
    fsp_type        = graphene.String(required=False)
    location_id     = graphene.Int(required=False)
    rerun           = graphene.Boolean(required=False)
    account_uuids   = graphene.List(graphene.UUID, required=False)


class GeneratePaylistInputType(OpenIMISMutation.Input):
    # Payroll / PaymentCycle are HistoryModels with UUID primary keys.
    payroll_id       = graphene.UUID(required=True)
    batch_type       = graphene.String(required=True)   # BANK / MNO
    payment_cycle_id = graphene.UUID(required=False)
    destination      = graphene.String(required=False)   # MUSE / GEPG — defaults to MUSE


class ApprovePaylistInputType(OpenIMISMutation.Input):
    paylist_uuid = graphene.UUID(required=True)


class SubmitPaylistInputType(OpenIMISMutation.Input):
    paylist_uuid = graphene.UUID(required=True)


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
            (data.get('fsp_name_icontains') or '').strip(),
            (data.get('account_number_icontains') or '').strip(),
            data.get('location_id'),
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
        for key in ('fsp_name_icontains', 'account_number_icontains'):
            if (data.get(key) or '').strip():
                filters[key] = data[key].strip()
        if data.get('location_id'):
            filters['location_id'] = int(data['location_id'])
        if data.get('rerun'):
            filters['rerun'] = bool(data['rerun'])
        if data.get('account_uuids'):
            filters['account_uuids'] = [str(u) for u in data['account_uuids']]
        result = BatchVerificationService(user).dispatch(filters)
        if not result.get('success'):
            raise Exception(result.get('error', 'Batch verification dispatch failed'))

    class Input(RunBatchVerificationInputType):
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


class RunBatchPreAuditMutation(BaseMutation):
    """Pre-audit every candidate matching the filters, not just a selection.

    Same right as the per-selection mutation: this widens the scope, it does not change
    what the check does or who may run it.
    """
    _mutation_class = "RunBatchPreAuditMutation"
    _mutation_module = TasafPaymentConfig.name

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require_perms(user, TasafPaymentConfig.gql_run_pre_audit_perms)
        has_filter = any([
            data.get('benefit_plan_id'),
            data.get('fsp_type'),
            data.get('location_id'),
            data.get('account_uuids'),
        ])
        if not has_filter:
            raise ValidationError(
                _("tasaf_payment.validation.batch_pre_audit_requires_filter")
            )

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import BatchPreAuditService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        filters = {}
        if data.get('benefit_plan_id'):
            filters['benefit_plan_id'] = str(data['benefit_plan_id'])
        if data.get('fsp_type'):
            filters['fsp_type'] = data['fsp_type']
        if data.get('location_id'):
            filters['location_id'] = int(data['location_id'])
        if data.get('rerun'):
            filters['rerun'] = bool(data['rerun'])
        if data.get('account_uuids'):
            filters['account_uuids'] = [str(u) for u in data['account_uuids']]
        result = BatchPreAuditService(user).dispatch(filters)
        if not result.get('success'):
            raise Exception(result.get('error', 'Batch pre-audit dispatch failed'))

    class Input(RunBatchPreAuditInputType):
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
        if data.get('batch_type') not in BatchType.values:
            raise ValidationError(_("tasaf_payment.validation.invalid_batch_type"))
        destination = data.get('destination')
        if destination and destination not in PaymentDestination.values:
            raise ValidationError(_("tasaf_payment.validation.invalid_destination"))

    @classmethod
    def _mutate(cls, user, **data):
        from tasaf_payment.services import PaylistService
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        result = PaylistService(user).generate(
            payroll_id=data['payroll_id'],
            batch_type=data['batch_type'],
            payment_cycle_id=data.get('payment_cycle_id'),
            destination=data.get('destination'),
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


# ─── Withdrawal charges ───────────────────────────────────────────────────────

class WithdrawalChargeInputType(OpenIMISMutation.Input):
    uuid = graphene.UUID(required=False)
    fsp_code = graphene.String(required=True)
    lower_amount = graphene.Decimal(required=True)
    upper_amount = graphene.Decimal(required=True)
    # 0 is valid and means "no charge" -- distinct from having no band at all.
    withdrawal = graphene.Decimal(required=True)
    effective_from = graphene.Date(required=False)
    effective_to = graphene.Date(required=False)


class SaveWithdrawalChargeMutation(OpenIMISMutation):
    """Create or update one tariff band."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "SaveWithdrawalChargeMutation"

    class Input(WithdrawalChargeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            from tasaf_payment.charges import normalise_fsp
            uuid_ = data.pop('uuid', None)
            if data['lower_amount'] > data['upper_amount']:
                return [{'message': _("Lower amount is above upper amount")}]
            data['fsp_code'] = normalise_fsp(data['fsp_code'])

            # Overlaps make the band that applies ambiguous, so reject rather than guess.
            clash = WithdrawalCharge.objects.filter(
                is_deleted=False, fsp_code=data['fsp_code'],
                lower_amount__lte=data['upper_amount'],
                upper_amount__gte=data['lower_amount'])
            if uuid_:
                clash = clash.exclude(uuid=uuid_)
            if clash.exists():
                return [{'message': _("Band overlaps an existing band for this FSP")}]

            from tasaf_payment.services import WithdrawalChargeService
            service = WithdrawalChargeService(user)

            if uuid_:
                obj = WithdrawalCharge.objects.filter(uuid=uuid_, is_deleted=False).first()
                if not obj:
                    return [{'message': _("Withdrawal charge not found")}]
                data['id'] = str(obj.id)
                if TasafPaymentConfig.charges_require_approval:
                    result = service.create_update_task(data)
                else:
                    result = service.update(data)
            else:
                result = service.create(data)
            return result if not result.get('success') else None
        except Exception as exc:
            return [{'message': str(exc)}]


class DeleteWithdrawalChargeInputType(OpenIMISMutation.Input):
    uuids = graphene.List(graphene.UUID, required=True)


class DeleteWithdrawalChargeMutation(OpenIMISMutation):
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "DeleteWithdrawalChargeMutation"

    class Input(DeleteWithdrawalChargeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            from tasaf_payment.services import WithdrawalChargeService
            service = WithdrawalChargeService(user)
            for obj in WithdrawalCharge.objects.filter(uuid__in=data.get('uuids', []),
                                                       is_deleted=False):
                service.delete({'id': str(obj.id)})
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class ImportWithdrawalChargesInputType(OpenIMISMutation.Input):
    # Raw CSV text: EPAYMENT_CODE, LOWER_AMOUNT, UPPER_AMOUNT, WITHDRAWAL
    csv_content = graphene.String(required=True)
    effective_from = graphene.Date(required=False)
    replace = graphene.Boolean(required=False)


class ImportWithdrawalChargesMutation(OpenIMISMutation):
    """Load a tariff CSV. Malformed rows are skipped and reported rather than aborting the
    import; gaps are reported for configuration but are not errors."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "ImportWithdrawalChargesMutation"

    class Input(ImportWithdrawalChargesInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            from tasaf_payment.charges import import_csv
            result = import_csv(data['csv_content'], user,
                                effective_from=data.get('effective_from'),
                                replace=bool(data.get('replace')))
            if result['skipped']:
                first = result['errors'][0]
                return [{'message': _("Imported %d row(s), skipped %d. First problem: line %s, %s")
                         % (result['imported'], result['skipped'],
                            first['line'], first['error'])}]
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class SaveFspMappingInputType(OpenIMISMutation.Input):
    uuid = graphene.UUID(required=False)
    fsp_name = graphene.String(required=True)
    fsp_code = graphene.String(required=True)


class SaveFspMappingMutation(OpenIMISMutation):
    """Add or edit an FSP display-name -> tariff-code mapping, so onboarding a new FSP is a
    UI action rather than a configuration change."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "SaveFspMappingMutation"

    class Input(SaveFspMappingInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            uuid_ = data.pop('uuid', None)
            if uuid_:
                obj = FspMapping.objects.filter(uuid=uuid_, is_deleted=False).first()
                if not obj:
                    return [{'message': _("FSP mapping not found")}]
                obj.fsp_name = data['fsp_name']
                obj.fsp_code = data['fsp_code']
                obj.save(username=user.username)
            else:
                FspMapping(**data).save(username=user.username)
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class DeleteFspMappingInputType(OpenIMISMutation.Input):
    uuids = graphene.List(graphene.UUID, required=True)


class DeleteFspMappingMutation(OpenIMISMutation):
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "DeleteFspMappingMutation"

    class Input(DeleteFspMappingInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            for obj in FspMapping.objects.filter(uuid__in=data.get('uuids', []), is_deleted=False):
                obj.delete(username=user.username)
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class SeedFspMappingsMutation(OpenIMISMutation):
    """Materialise the shipped alias defaults as editable rows, once."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "SeedFspMappingsMutation"

    class Input(OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            from tasaf_payment.charges import seed_fsp_mappings
            seed_fsp_mappings(user)
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class ChargeBandInputType(graphene.InputObjectType):
    lower_amount = graphene.Decimal(required=True)
    upper_amount = graphene.Decimal(required=True)
    withdrawal = graphene.Decimal(required=True)


class SaveFspChargesInputType(OpenIMISMutation.Input):
    fsp_code = graphene.String(required=True)
    bands = graphene.List(ChargeBandInputType, required=True)
    effective_from = graphene.Date(required=False)


class SaveFspChargesMutation(OpenIMISMutation):
    """Apply a whole FSP tariff in one action, like the PMT formula: the config editor sends
    every band for one FSP and this replaces the set atomically. A partially applied tariff
    would misprice payments, so it is all-or-nothing.

    With charges_require_approval on, the set is parked as a tasks_management Task and nothing
    changes until a second user approves.
    """
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "SaveFspChargesMutation"

    class Input(SaveFspChargesInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            _require_perms(user, TasafPaymentConfig.gql_withdrawal_charge_manage_perms)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            from tasaf_payment.charges import (
                FSP_CHARGES_EVENT, apply_band_set, normalise_fsp, validate_band_set,
            )
            fsp_code = normalise_fsp(data['fsp_code'])
            bands = [dict(b) for b in (data.get('bands') or [])]
            errors = validate_band_set(bands)
            if errors:
                return [{'message': '; '.join(errors)}]

            effective_from = data.get('effective_from')
            if not TasafPaymentConfig.charges_require_approval:
                apply_band_set(fsp_code, bands, user, effective_from)
                return None

            from tasks_management.services import TaskService
            payload = {
                'fsp_code': fsp_code,
                'effective_from': str(effective_from) if effective_from else None,
                'bands': [{k: str(v) for k, v in b.items()} for b in bands],
            }
            result = TaskService(user).create({
                'source': 'tasaf_payment',
                'entity_id': None,
                'entity_type': None,
                'business_event': FSP_CHARGES_EVENT,
                'business_status': {},
                'data': payload,
            })
            if not result.get('success'):
                return [{'message': result.get('detail') or _("Could not queue the change")}]
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class SaveFspProfileInputType(OpenIMISMutation.Input):
    fsp_code = graphene.String(required=True)
    bank_name = graphene.String(required=True)
    fsp_type = graphene.String(required=True)
    bic = graphene.String(required=True)


class SaveFspProfileMutation(OpenIMISMutation):
    """Propose MUSE routing data (bank name, channel, BIC) for one FSP; applied on approval."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "SaveFspProfileMutation"

    class Input(SaveFspProfileInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        from tasaf_payment.muse_setup import propose_profile
        try:
            _require_perms(user, TasafPaymentConfig.gql_muse_settings_propose_perms)
            propose_profile(user, data['fsp_code'], data['bank_name'], data['fsp_type'], data['bic'])
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class SaveMuseSettingsInputType(OpenIMISMutation.Input):
    institution_code = graphene.String(required=False)
    payer_account = graphene.String(required=False)
    sub_budget_class = graphene.Int(required=False)
    unapplied_sub_budget_class = graphene.Int(required=False)
    payment_desc = graphene.String(required=False)
    is_stp = graphene.Boolean(required=False)
    gl_accounts = graphene.JSONString(required=False)


class SaveMuseSettingsMutation(OpenIMISMutation):
    """Propose the accounting values MUSE supplies; applied on approval. Blank values are
    allowed while waiting for MUSE; the readiness check reports what is still missing."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "SaveMuseSettingsMutation"

    class Input(SaveMuseSettingsInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        from tasaf_payment.muse_setup import propose_settings
        try:
            _require_perms(user, TasafPaymentConfig.gql_muse_settings_propose_perms)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            propose_settings(user, data)
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class MuseChangeInputType(OpenIMISMutation.Input):
    change_id = graphene.UUID(required=True)
    comment = graphene.String(required=False)


class ApproveMuseChangeMutation(OpenIMISMutation):
    """Approve a proposed MUSE settings / FSP routing change. Not by its requester."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "ApproveMuseChangeMutation"

    class Input(MuseChangeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        from tasaf_payment.muse_setup import decide
        try:
            _require_perms(user, TasafPaymentConfig.gql_muse_settings_approve_perms)
            decide(user, data['change_id'], True, data.get('comment'))
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class RejectMuseChangeMutation(OpenIMISMutation):
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "RejectMuseChangeMutation"

    class Input(MuseChangeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        from tasaf_payment.muse_setup import decide
        try:
            _require_perms(user, TasafPaymentConfig.gql_muse_settings_approve_perms)
            decide(user, data['change_id'], False, data.get('comment'))
            return None
        except Exception as exc:
            return [{'message': str(exc)}]


class CancelMuseChangeMutation(OpenIMISMutation):
    """Withdraw a proposed change: its requester, or the engine's cancel right."""
    _mutation_module = TasafPaymentConfig.name
    _mutation_class = "CancelMuseChangeMutation"

    class Input(MuseChangeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        from tasaf_payment.muse_setup import cancel
        try:
            _require_perms(user, TasafPaymentConfig.gql_muse_settings_propose_perms)
            cancel(user, data['change_id'], data.get('comment'))
            return None
        except Exception as exc:
            return [{'message': str(exc)}]
