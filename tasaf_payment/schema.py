import graphene
import graphene_django_optimizer as gql_optimizer
from gettext import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, Count, Sum

from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter
from tasaf_payment.apps import TasafPaymentConfig
from tasaf_payment.gql_mutations import (
    SaveWithdrawalChargeMutation,
    DeleteWithdrawalChargeMutation,
    ImportWithdrawalChargesMutation,
    SaveFspMappingMutation,
    SaveFspProfileMutation, DeleteFspMutation,
    SaveMuseSettingsMutation,
    ApproveMuseChangeMutation,
    RejectMuseChangeMutation,
    CancelMuseChangeMutation,
    DeleteFspMappingMutation,
    SeedFspMappingsMutation,
    SaveFspChargesMutation,
    CreatePaymentAccountMutation,
    UpdatePaymentAccountMutation,
    DeletePaymentAccountMutation,
    RunVerificationMutation,
    ApprovePaymentAccountsMutation,
    RunBatchVerificationMutation,
    RunPreAuditMutation,
    RunBatchPreAuditMutation,
    GeneratePaylistMutation,
    ApprovePaylistMutation,
    SubmitPaylistMutation,
)
from tasaf_payment.gql_queries import (
    WithdrawalChargeGQLType,
    ChargeGapGQLType,
    FspMappingGQLType,
    FspProviderGQLType,
    MuseSettingsGQLType,
    MuseReadinessGQLType,
    PaymentAccountGQLType,
    VerificationRecordGQLType,
    MuseVerificationRecordGQLType,
    PaylistGQLType,
    PaylistItemGQLType,
    MuseLogEntryGQLType,
    ReturnFeedbackGQLType,
)
from tasaf_payment.models import (
    WithdrawalCharge,
    FspMapping,
    PaymentAccount,
    VerificationRecord,
    MuseVerificationRecord,
    Paylist,
    PaylistItem,
    ReturnFeedback,
    VerificationStatus,
    PaylistStatus,
    PaylistItemStatus,
    PaymentDestination,
    PAYLIST_IN_FLIGHT_STATUSES,
)



class DashboardAccountStatGQLType(graphene.ObjectType):
    status = graphene.String()
    count = graphene.Int()


class DashboardPaylistStatGQLType(graphene.ObjectType):
    status = graphene.String()
    count = graphene.Int()           # number of paylists in this status
    beneficiaries = graphene.Int()   # number of paylist items
    amount = graphene.Float()        # summed item amount (TZS)


class PaymentDashboardSummaryGQLType(graphene.ObjectType):
    accounts = graphene.List(DashboardAccountStatGQLType)
    paylists = graphene.List(DashboardPaylistStatGQLType)
    total_accounts = graphene.Int()
    total_paylists = graphene.Int()
    in_process_amount = graphene.Float()   # amount on paylists still with MUSE
    paid_amount = graphene.Float()         # amount on PROCESSED items (disbursed)




class EpaymentFspRowGQLType(graphene.ObjectType):
    epayment_code = graphene.String()
    households = graphene.Int()
    withdrawal_charges = graphene.Float()
    pct_payment = graphene.Float()
    child_grant = graphene.Float()
    disability_grant = graphene.Float()
    pwp_payment = graphene.Float()        # no PWP concept exists yet — always 0
    ei_payment = graphene.Float()        # Economic Inclusion — no data yet, always 0
    has_child = graphene.Int()
    primary_student = graphene.Int()
    secondary_student = graphene.Int()
    component_total = graphene.Float()    # pct_breakdown.raw_total — PRE household cap
    total_paid = graphene.Float()
    items = graphene.Int()


class EpaymentSummaryByFspGQLType(graphene.ObjectType):
    rows = graphene.List(EpaymentFspRowGQLType)
    totals = graphene.Field(EpaymentFspRowGQLType)


def paylist_item_filters(**kwargs):
    filters = []
    if kwargs.get("paylist_uuid"):
        filters.append(Q(paylist__uuid=kwargs["paylist_uuid"]))
    if kwargs.get("status"):
        filters.append(Q(status=kwargs["status"]))
    if kwargs.get("muse_reference__icontains"):
        filters.append(Q(muse_reference__icontains=kwargs["muse_reference__icontains"].strip()))
    if kwargs.get("account_number"):
        filters.append(Q(payment_account__account_number__icontains=kwargs["account_number"].strip()))
    if kwargs.get("fsp_name"):
        filters.append(Q(payment_account__fsp_name__icontains=kwargs["fsp_name"].strip()))
    if kwargs.get("benefit_code"):
        filters.append(Q(benefit_consumption__code__icontains=kwargs["benefit_code"].strip()))
    if kwargs.get("hhid"):
        filters.append(Q(payment_account__group_beneficiary__group__code__icontains=kwargs["hhid"].strip()))
    if kwargs.get("location_id"):
        from tasaf_payment.services import location_descendants_q
        filters.append(location_descendants_q(
            kwargs["location_id"], base='payment_account__group_beneficiary__group__location'))
    return filters


class Query(graphene.ObjectType):

    verification_batch_preview = graphene.Field(
        graphene.JSONString,
        fsp_type=graphene.String(),
        fsp_name_icontains=graphene.String(),
        account_number_icontains=graphene.String(),
        location_id=graphene.Int(),
        sample_rows=graphene.Int(),
        description="The GovESB messages 'Verify all matching' would publish for these filters. "
                    "Read-only: nothing is sent and no account changes.",
    )

    paylist_generation_preview = graphene.Field(
        graphene.JSONString,
        payroll_id=graphene.UUID(required=True),
        batch_type=graphene.String(required=True),
        destination=graphene.String(),
        fsp_code=graphene.String(),
        description="What generating this batch would include, leave out and warn about. Read-only.",
    )

    paylist_muse_preview = graphene.Field(
        graphene.JSONString,
        paylist_uuid=graphene.UUID(required=True),
        sample_rows=graphene.Int(),
        description="The MUSE BULK_PAYMENT message a submit of this paylist would carry, and what "
                    "MUSE's schema would reject in it. Read-only: nothing is sent.",
    )

    epayment_fsp_items = OrderedDjangoFilterConnectionField(
        PaylistItemGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        epayment_code=graphene.String(required=True),
        payment_cycle_id=graphene.UUID(required=False),
        date_from=graphene.Date(required=False),
        date_to=graphene.Date(required=False),
        destination=graphene.String(required=False),
        include_unpaid=graphene.Boolean(required=False),
        description="Paid items behind one FSP row of the e-Payment summary, for "
                    "reconciliation. Defaults to PROCESSED only, matching the report.",
    )

    epayment_beneficiary_items = OrderedDjangoFilterConnectionField(
        PaylistItemGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        payment_account_uuid=graphene.UUID(required=True),
        description="Every payment made to one beneficiary's account, all statuses, "
                    "so an auditor can see their full history from the FSP drill-down.",
    )

    epayment_summary_by_fsp_export = graphene.String(
        payment_cycle_id=graphene.UUID(required=False),
        date_from=graphene.Date(required=False),
        date_to=graphene.Date(required=False),
        destination=graphene.String(required=False),
        description="Writes the FSP summary as .xlsx and returns the export name for "
                    "core's /api/core/fetch_export.",
    )

    epayment_summary_by_fsp = graphene.Field(
        EpaymentSummaryByFspGQLType,
        payment_cycle_id=graphene.UUID(required=False),
        date_from=graphene.Date(required=False),
        date_to=graphene.Date(required=False),
        destination=graphene.String(required=False),
        description="Summary of e-Payment by FSP. Counts only PROCESSED (confirmed paid) items.",
    )

    # ── Payment accounts ──────────────────────────────────────────────────────
    payment_account = OrderedDjangoFilterConnectionField(
        PaymentAccountGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        uuid=graphene.UUID(),
        group_beneficiary_uuid=graphene.UUID(),
        location_id=graphene.Int(),
    )

    # ── MUSE verification records ─────────────────────────────────────────────
    muse_verification_record = OrderedDjangoFilterConnectionField(
        MuseVerificationRecordGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        payment_account_uuid=graphene.UUID(),
        # verification_type, result handled by filter_fields
    )

    # ── Withdrawal charges (tariff table) ─────────────────────────────────────
    withdrawal_charge = OrderedDjangoFilterConnectionField(
        WithdrawalChargeGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    # FSP display-name -> tariff-code map, editable so a new FSP can be onboarded in the UI.
    fsp_mapping = OrderedDjangoFilterConnectionField(
        FspMappingGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    fsp_providers = graphene.List(FspProviderGQLType)
    muse_settings = graphene.Field(MuseSettingsGQLType)
    muse_readiness = graphene.Field(MuseReadinessGQLType)
    muse_change_requests = graphene.Field(graphene.JSONString, status=graphene.String())
    # Current bands for one FSP, for the config editor.
    fsp_band_set = graphene.List(WithdrawalChargeGQLType, fsp_code=graphene.String(required=True))

    # ── Paylists ──────────────────────────────────────────────────────────────
    paylist = OrderedDjangoFilterConnectionField(
        PaylistGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        payroll_id=graphene.Int(),
        payment_cycle_id=graphene.Int(),
        # batch_type, status handled by filter_fields
    )

    paylist_item = OrderedDjangoFilterConnectionField(
        PaylistItemGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        paylist_uuid=graphene.UUID(),
        account_number=graphene.String(),
        fsp_name=graphene.String(),
        benefit_code=graphene.String(),
        hhid=graphene.String(),
        location_id=graphene.Int(),
        # status, muse_reference handled by filter_fields
    )
    paylist_items_export = graphene.String(
        paylist_uuid=graphene.UUID(required=True),
        account_number=graphene.String(),
        fsp_name=graphene.String(),
        benefit_code=graphene.String(),
        hhid=graphene.String(),
        location_id=graphene.Int(),
        status=graphene.String(),
        muse_reference=graphene.String(),
        description="The paylist's items matching the filters as CSV; returns the export name for "
                    "core's /api/core/fetch_export.",
    )
    paylist_muse_log = graphene.List(
        MuseLogEntryGQLType,
        paylist_uuid=graphene.UUID(required=True),
        limit=graphene.Int(),
        description="Send attempts and MUSE's messages for one paylist, newest first.",
    )

    # ── Return feedback ───────────────────────────────────────────────────────
    return_feedback = OrderedDjangoFilterConnectionField(
        ReturnFeedbackGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        paylist_uuid=graphene.UUID(),
    )

    # ── Legacy (read-only audit trail) ────────────────────────────────────────
    verification_record = OrderedDjangoFilterConnectionField(
        VerificationRecordGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        payment_account_uuid=graphene.UUID(),
    )

    # ── Dashboard summary (counts + amounts in one query) ─────────────────────
    payment_dashboard_summary = graphene.Field(PaymentDashboardSummaryGQLType)

    # ─── Resolvers ───────────────────────────────────────────────────────────

    def resolve_payment_account(self, info, **kwargs):
        Query._check_any_permission(info.context.user, [
            TasafPaymentConfig.gql_payment_account_search_perms,
            TasafPaymentConfig.gql_pre_audit_search_perms,
        ])
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        if kwargs.get("uuid"):
            filters.append(Q(uuid=kwargs["uuid"]))
        if kwargs.get("group_beneficiary_uuid"):
            filters.append(Q(group_beneficiary_id=kwargs["group_beneficiary_uuid"]))
        if kwargs.get("pre_audit_status"):
            filters.append(Q(pre_audit_status=kwargs["pre_audit_status"]))
        if kwargs.get("active_check_status"):
            filters.append(Q(active_check_status=kwargs["active_check_status"]))
        if kwargs.get("location_id"):
            from tasaf_payment.services import location_descendants_q
            filters.append(location_descendants_q(kwargs["location_id"]))

        return gql_optimizer.query(PaymentAccount.objects.filter(*filters), info)

    def resolve_muse_verification_record(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_muse_verification_search_perms)
        filters = [Q(is_deleted=False)]

        if kwargs.get("payment_account_uuid"):
            filters.append(Q(payment_account__uuid=kwargs["payment_account_uuid"]))
        if kwargs.get("verification_type"):
            filters.append(Q(verification_type=kwargs["verification_type"]))
        if kwargs.get("result"):
            filters.append(Q(result=kwargs["result"]))

        return gql_optimizer.query(
            MuseVerificationRecord.objects.filter(*filters).order_by('-received_at'),
            info,
        )

    def resolve_paylist(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_paylist_search_perms)
        filters = [Q(is_deleted=False)]

        if kwargs.get("batch_type"):
            filters.append(Q(batch_type=kwargs["batch_type"]))
        if kwargs.get("status"):
            filters.append(Q(status=kwargs["status"]))
        if kwargs.get("payroll_id"):
            filters.append(Q(payroll_id=kwargs["payroll_id"]))
        if kwargs.get("payment_cycle_id"):
            filters.append(Q(payment_cycle_id=kwargs["payment_cycle_id"]))

        return gql_optimizer.query(Paylist.objects.filter(*filters), info)

    def resolve_paylist_item(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_paylist_search_perms)
        filters = [Q(is_deleted=False)]

        filters.extend(paylist_item_filters(**kwargs))
        return gql_optimizer.query(PaylistItem.objects.filter(*filters), info)

    def resolve_paylist_items_export(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_paylist_search_perms)
        from tasaf_payment.reports import export_paylist_items
        if kwargs.get("muse_reference"):
            kwargs["muse_reference__icontains"] = kwargs.pop("muse_reference")
        items = PaylistItem.objects.filter(*paylist_item_filters(**kwargs), is_deleted=False)
        return export_paylist_items(info.context.user, items)

    def resolve_paylist_muse_log(self, info, paylist_uuid, limit=None):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_paylist_search_perms)
        try:
            from muse_payment_adaptor.models import MuseTransactionLog
        except ImportError:
            return []
        paylist = Paylist.objects.filter(uuid=paylist_uuid).first()
        if not paylist:
            return []
        match = Q(paylist_uuid=str(paylist.uuid))
        if paylist.muse_msg_id:
            match |= Q(msg_id=paylist.muse_msg_id)
        rows = MuseTransactionLog.objects.filter(match).order_by('-created_at', '-id')[:min(limit or 100, 500)]
        return [MuseLogEntryGQLType(
            created_at=r.created_at, direction=r.direction, transaction_type=r.transaction_type,
            status=r.status, attempt_number=r.attempt_number, msg_id=r.msg_id,
            muse_reference=r.muse_reference, esb_request_id=r.esb_request_id,
            http_status_code=r.http_status_code, item_count=r.item_count,
            amount=float(r.amount) if r.amount is not None else None, benefit_code=r.benefit_code,
            error_message=r.error_message, response_body=(r.response_body or '')[:2000],
        ) for r in rows]

    def resolve_return_feedback(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_return_feedback_search_perms)
        filters = [Q(is_deleted=False)]

        if kwargs.get("paylist_uuid"):
            filters.append(Q(paylist_item__paylist__uuid=kwargs["paylist_uuid"]))

        return gql_optimizer.query(ReturnFeedback.objects.filter(*filters), info)

    def resolve_verification_record(self, info, **kwargs):
        # Legacy NIDA audit trail — read-only
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_payment_account_search_perms)
        filters = []
        if kwargs.get("payment_account_uuid"):
            filters.append(Q(payment_account__uuid=kwargs["payment_account_uuid"]))
        return gql_optimizer.query(VerificationRecord.objects.filter(*filters), info)

    def resolve_epayment_beneficiary_items(self, info, **kwargs):
        """One beneficiary's full payment history, deliberately unfiltered by status.

        The FSP drill-down is scoped to PROCESSED so it reconciles; this level is the
        opposite — an auditor looking at a single household wants everything that was
        ever attempted for them, including what came back.
        """
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_reports_perms)

        return gql_optimizer.query(
            PaylistItem.objects.filter(
                is_deleted=False,
                payment_account__uuid=kwargs['payment_account_uuid'],
            ),
            info,
        )

    def resolve_epayment_fsp_items(self, info, **kwargs):
        """The individual payments behind one FSP row, so an auditor can reconcile.

        Same scope rule as the summary — PROCESSED only — so the rows here add up to
        the figure clicked on. ``include_unpaid`` widens it to returned/unapplied/
        pending for investigating a discrepancy, and is off by default precisely so
        the default view reconciles.
        """
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_reports_perms)
        from tasaf_payment.reports import fsp_names_for_code

        names = fsp_names_for_code(kwargs.get('epayment_code'))
        filters = [Q(is_deleted=False), Q(payment_account__fsp_name__in=names)]

        if not kwargs.get('include_unpaid'):
            filters.append(Q(status=PaylistItemStatus.PROCESSED))
        if kwargs.get('payment_cycle_id'):
            filters.append(Q(paylist__payment_cycle_id=kwargs['payment_cycle_id']))
        if kwargs.get('destination'):
            filters.append(Q(paylist__destination=kwargs['destination']))
        if kwargs.get('date_from'):
            filters.append(Q(settled_at__gte=kwargs['date_from']))
        if kwargs.get('date_to'):
            filters.append(Q(settled_at__lte=kwargs['date_to']))

        return gql_optimizer.query(PaylistItem.objects.filter(*filters), info)

    def resolve_epayment_summary_by_fsp_export(self, info, **kwargs):
        """CSV export via core's export transport."""
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_reports_perms)
        from tasaf_payment.reports import export_epayment_summary_by_fsp

        return export_epayment_summary_by_fsp(
            info.context.user,
            payment_cycle_id=kwargs.get('payment_cycle_id'),
            date_from=kwargs.get('date_from'),
            date_to=kwargs.get('date_to'),
            destination=kwargs.get('destination'),
        )

    def resolve_epayment_summary_by_fsp(self, info, **kwargs):
        """Summary of e-Payment by FSP."""
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_reports_perms)
        from tasaf_payment.reports import epayment_summary_by_fsp

        result = epayment_summary_by_fsp(
            payment_cycle_id=kwargs.get('payment_cycle_id'),
            date_from=kwargs.get('date_from'),
            date_to=kwargs.get('date_to'),
            destination=kwargs.get('destination'),
        )
        to_row = lambda r: EpaymentFspRowGQLType(  # noqa: E731
            epayment_code=r['epayment_code'],
            households=r['households'],
            withdrawal_charges=float(r['withdrawal_charges']),
            pct_payment=float(r['pct_payment']),
            child_grant=float(r['child_grant']),
            disability_grant=float(r['disability_grant']),
            pwp_payment=float(r['pwp_payment']),
            ei_payment=float(r["ei_payment"]),
            has_child=r['has_child'],
            primary_student=r['primary_student'],
            secondary_student=r['secondary_student'],
            component_total=float(r['component_total']),
            total_paid=float(r['total_paid']),
            items=r['items'],
        )
        return EpaymentSummaryByFspGQLType(
            rows=[to_row(r) for r in result['rows']],
            totals=to_row(result['totals']),
        )

    def resolve_payment_dashboard_summary(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_dashboard_perms)

        # Account counts by verification_status (accounts hold no money → count only).
        acct_rows = (
            PaymentAccount.objects.filter(is_deleted=False)
            .values("verification_status").annotate(c=Count("id"))
        )
        acct_map = {row["verification_status"]: row["c"] for row in acct_rows}
        accounts = [
            DashboardAccountStatGQLType(status=st.name, count=acct_map.get(st.value, 0))
            for st in VerificationStatus
        ]

        # Paylist counts by status.
        pl_rows = (
            Paylist.objects.filter(is_deleted=False)
            .values("status").annotate(c=Count("id"))
        )
        pl_count_map = {row["status"]: row["c"] for row in pl_rows}

        # Beneficiary counts + summed amounts, grouped by the owning paylist's status.
        item_rows = (
            PaylistItem.objects.filter(is_deleted=False, paylist__is_deleted=False)
            .values("paylist__status").annotate(b=Count("id"), amt=Sum("amount"))
        )
        item_map = {row["paylist__status"]: (row["b"], row["amt"] or 0) for row in item_rows}

        paylists = []
        for st in PaylistStatus:
            beneficiaries, amount = item_map.get(st.value, (0, 0))
            paylists.append(DashboardPaylistStatGQLType(
                status=st.value,
                count=pl_count_map.get(st.value, 0),
                beneficiaries=beneficiaries,
                amount=float(amount),
            ))

        # Headline totals.
        in_process = (
            PaylistItem.objects.filter(
                is_deleted=False, paylist__is_deleted=False, paylist__status__in=PAYLIST_IN_FLIGHT_STATUSES,
            ).aggregate(s=Sum("amount"))["s"] or 0
        )
        paid = (
            PaylistItem.objects.filter(is_deleted=False, status=PaylistItemStatus.PROCESSED)
            .aggregate(s=Sum("amount"))["s"] or 0
        )

        return PaymentDashboardSummaryGQLType(
            accounts=accounts,
            paylists=paylists,
            total_accounts=sum(acct_map.values()),
            total_paylists=sum(pl_count_map.values()),
            in_process_amount=float(in_process),
            paid_amount=float(paid),
        )

    def resolve_withdrawal_charge(self, info, **kwargs):
        Query._check_permissions(info.context.user,
                                 TasafPaymentConfig.gql_withdrawal_charge_search_perms)
        return WithdrawalCharge.objects.filter(is_deleted=False)

    def resolve_fsp_mapping(self, info, **kwargs):
        Query._check_permissions(info.context.user,
                                 TasafPaymentConfig.gql_withdrawal_charge_search_perms)
        return FspMapping.objects.filter(is_deleted=False)

    def resolve_fsp_providers(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_withdrawal_charge_search_perms)
        from tasaf_payment.muse_setup import provider_list
        return [FspProviderGQLType(**row) for row in provider_list()]

    def resolve_muse_settings(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_muse_settings_search_perms)
        from tasaf_payment.muse_setup import SETTINGS_FIELDS, get_settings
        s = get_settings()
        if s is None:
            return None
        return MuseSettingsGQLType(date_updated=s.date_updated, environment=s.environment,
                                   **{f: getattr(s, f) for f in SETTINGS_FIELDS})

    def resolve_muse_change_requests(self, info, status=None, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_muse_settings_search_perms)
        from tasaf_payment.muse_setup import change_rows
        user = info.context.user
        rows = change_rows(status=status)
        me = getattr(user, 'username', None)
        can_approve = user.has_perms(TasafPaymentConfig.gql_muse_settings_approve_perms)
        can_propose = user.has_perms(TasafPaymentConfig.gql_muse_settings_propose_perms)
        for r in rows:
            mine = r['requested_by'] == me
            pending = r['status'] == 'PENDING'
            r['can_decide'] = pending and can_approve and not mine
            r['can_cancel'] = pending and can_propose and mine
        return rows

    def resolve_muse_readiness(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_muse_settings_search_perms)
        from tasaf_payment.muse_setup import readiness
        return MuseReadinessGQLType(**readiness())

    def resolve_fsp_band_set(self, info, fsp_code, **kwargs):
        Query._check_permissions(info.context.user,
                                 TasafPaymentConfig.gql_withdrawal_charge_search_perms)
        from tasaf_payment.charges import normalise_fsp
        return (WithdrawalCharge.objects
                .filter(is_deleted=False, fsp_code=normalise_fsp(fsp_code))
                .order_by('lower_amount'))

    def resolve_paylist_generation_preview(self, info, payroll_id, batch_type, destination=None, fsp_code=None,
                                           **kwargs):
        from tasaf_payment.services import PaylistService

        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_generate_paylist_perms)
        return PaylistService(info.context.user).preview_generation(payroll_id, batch_type, destination, fsp_code or None)

    def resolve_paylist_muse_preview(self, info, paylist_uuid, sample_rows=20, **kwargs):
        from tasaf_payment.muse_message import preview

        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_paylist_search_perms)
        paylist = Paylist.objects.filter(id=paylist_uuid, is_deleted=False).first()
        if paylist is None:
            raise ValueError(_("tasaf_payment.error.paylist_not_found"))
        if paylist.destination != PaymentDestination.MUSE:
            raise ValueError(_("tasaf_payment.error.muse_preview_not_muse"))
        return preview(paylist, sample_rows=max(0, min(int(sample_rows or 0), 200)))

    def resolve_verification_batch_preview(self, info, sample_rows=20, **kwargs):
        from tasaf_payment.services import MuseVerificationDispatchService
        from tasaf_payment.tasks import _build_account_queryset

        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_run_verification_perms)
        filters = {k: v for k, v in kwargs.items() if v not in (None, '')}
        if not filters:
            raise ValueError(_("tasaf_payment.validation.batch_verification_requires_filter"))
        ids = list(_build_account_queryset(filters).values_list('id', flat=True))
        return MuseVerificationDispatchService(info.context.user).preview(
            ids, sample_rows=max(0, min(int(sample_rows or 0), 200)))

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError(_("Unauthorized"))

    @staticmethod
    def _check_any_permission(user, perm_sets):
        """Authorise when the user holds ANY of the given permission sets.

        `has_perms` is all-or-nothing, so a plain union would demand every right at once.
        """
        if type(user) is not AnonymousUser and user.id:
            for perms in perm_sets:
                if perms and user.has_perms(perms):
                    return
        raise PermissionError(_("Unauthorized"))


class Mutation(graphene.ObjectType):
    # Withdrawal charges (tariff table)
    save_withdrawal_charge = SaveWithdrawalChargeMutation.Field()
    delete_withdrawal_charge = DeleteWithdrawalChargeMutation.Field()
    import_withdrawal_charges = ImportWithdrawalChargesMutation.Field()
    save_fsp_mapping = SaveFspMappingMutation.Field()
    save_fsp_profile = SaveFspProfileMutation.Field()
    delete_fsp = DeleteFspMutation.Field()
    save_muse_settings = SaveMuseSettingsMutation.Field()
    approve_muse_change = ApproveMuseChangeMutation.Field()
    reject_muse_change = RejectMuseChangeMutation.Field()
    cancel_muse_change = CancelMuseChangeMutation.Field()
    delete_fsp_mapping = DeleteFspMappingMutation.Field()
    seed_fsp_mappings = SeedFspMappingsMutation.Field()
    save_fsp_charges = SaveFspChargesMutation.Field()

    # CRUD
    create_payment_account = CreatePaymentAccountMutation.Field()
    update_payment_account = UpdatePaymentAccountMutation.Field()
    delete_payment_account = DeletePaymentAccountMutation.Field()
    # Verification
    run_verification         = RunVerificationMutation.Field()
    approve_payment_accounts = ApprovePaymentAccountsMutation.Field()
    run_batch_verification   = RunBatchVerificationMutation.Field()
    # Pre-audit
    run_pre_audit       = RunPreAuditMutation.Field()
    run_batch_pre_audit = RunBatchPreAuditMutation.Field()
    # Paylist
    generate_paylist = GeneratePaylistMutation.Field()
    approve_paylist  = ApprovePaylistMutation.Field()
    submit_paylist   = SubmitPaylistMutation.Field()
