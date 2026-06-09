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
    CreatePaymentAccountMutation,
    UpdatePaymentAccountMutation,
    DeletePaymentAccountMutation,
    RunVerificationMutation,
    ApprovePaymentAccountsMutation,
    RunBatchVerificationMutation,
    ResubmitFailedAccountsMutation,
    RunPreAuditMutation,
    GeneratePaylistMutation,
    ApprovePaylistMutation,
    SubmitPaylistMutation,
    RouteToCorrectionMutation,
)
from tasaf_payment.gql_queries import (
    PaymentAccountGQLType,
    VerificationRecordGQLType,
    MuseVerificationRecordGQLType,
    PaylistGQLType,
    PaylistItemGQLType,
    ReturnFeedbackGQLType,
)
from tasaf_payment.models import (
    PaymentAccount,
    VerificationRecord,
    MuseVerificationRecord,
    Paylist,
    PaylistItem,
    ReturnFeedback,
    VerificationStatus,
    PaylistStatus,
    PaylistItemStatus,
)


# ── Dashboard summary GQL types ────────────────────────────────────────────────
# One round-trip for the Payment Operations dashboard: per-status counts (accounts
# carry no money, so count-only) plus per-paylist-status beneficiary counts and
# summed amounts (amounts only exist on PaylistItem), and two headline totals.

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
    in_process_amount = graphene.Float()   # amount on SUBMITTED paylists (sent to MUSE)
    paid_amount = graphene.Float()         # amount on PROCESSED items (disbursed)


class Query(graphene.ObjectType):

    # ── Payment accounts ──────────────────────────────────────────────────────
    payment_account = OrderedDjangoFilterConnectionField(
        PaymentAccountGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        uuid=graphene.UUID(),
        group_beneficiary_uuid=graphene.UUID(),
    )

    # ── MUSE verification records ─────────────────────────────────────────────
    muse_verification_record = OrderedDjangoFilterConnectionField(
        MuseVerificationRecordGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        payment_account_uuid=graphene.UUID(),
        # verification_type, result handled by filter_fields
    )

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
        # status handled by filter_fields
    )

    # ── Return feedback ───────────────────────────────────────────────────────
    return_feedback = OrderedDjangoFilterConnectionField(
        ReturnFeedbackGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        paylist_uuid=graphene.UUID(),
        # feedback_type handled by filter_fields
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
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_payment_account_search_perms)
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

        if kwargs.get("paylist_uuid"):
            filters.append(Q(paylist__uuid=kwargs["paylist_uuid"]))
        if kwargs.get("status"):
            filters.append(Q(status=kwargs["status"]))

        return gql_optimizer.query(PaylistItem.objects.filter(*filters), info)

    def resolve_return_feedback(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_return_feedback_search_perms)
        filters = [Q(is_deleted=False)]

        if kwargs.get("paylist_uuid"):
            filters.append(Q(paylist_item__paylist__uuid=kwargs["paylist_uuid"]))
        if kwargs.get("feedback_type"):
            filters.append(Q(feedback_type=kwargs["feedback_type"]))

        return gql_optimizer.query(ReturnFeedback.objects.filter(*filters), info)

    def resolve_verification_record(self, info, **kwargs):
        # Legacy NIDA audit trail — read-only
        Query._check_permissions(info.context.user, TasafPaymentConfig.gql_payment_account_search_perms)
        filters = []
        if kwargs.get("payment_account_uuid"):
            filters.append(Q(payment_account__uuid=kwargs["payment_account_uuid"]))
        return gql_optimizer.query(VerificationRecord.objects.filter(*filters), info)

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
                is_deleted=False, paylist__is_deleted=False, paylist__status=PaylistStatus.SUBMITTED,
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

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError(_("Unauthorized"))


class Mutation(graphene.ObjectType):
    # CRUD
    create_payment_account = CreatePaymentAccountMutation.Field()
    update_payment_account = UpdatePaymentAccountMutation.Field()
    delete_payment_account = DeletePaymentAccountMutation.Field()
    # Verification
    run_verification         = RunVerificationMutation.Field()
    approve_payment_accounts = ApprovePaymentAccountsMutation.Field()
    run_batch_verification   = RunBatchVerificationMutation.Field()
    resubmit_failed_accounts = ResubmitFailedAccountsMutation.Field()
    route_to_correction      = RouteToCorrectionMutation.Field()
    # Pre-audit
    run_pre_audit = RunPreAuditMutation.Field()
    # Paylist
    generate_paylist = GeneratePaylistMutation.Field()
    approve_paylist  = ApprovePaylistMutation.Field()
    submit_paylist   = SubmitPaylistMutation.Field()
