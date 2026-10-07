import django_filters
import graphene
from django.db.models import Count, Sum
from graphene_django import DjangoObjectType
from graphene_django.filter import TypedFilter

from core import ExtendedConnection
from tasaf_payment.models import (
    PaylistItemStatus,
    WithdrawalCharge,
    FspMapping,
    PaymentAccount,
    VerificationStatus,
    VerificationRecord,
    MuseVerificationRecord,
    Paylist,
    PaylistItem,
    ReturnFeedback,
)


PaymentAccountVerificationStatusEnum = graphene.Enum.from_enum(VerificationStatus)


class PaymentAccountFilterSet(django_filters.FilterSet):
    verification_status = TypedFilter(
        method="filter_verification_status",
        input_type=PaymentAccountVerificationStatusEnum,
    )

    verification_status_in = TypedFilter(
        method="filter_verification_status_in",
        input_type=graphene.List(PaymentAccountVerificationStatusEnum),
    )

    @staticmethod
    def _to_status_value(value):
        normalized = getattr(value, "value", value)
        if isinstance(normalized, str) and normalized in VerificationStatus.__members__:
            return VerificationStatus[normalized].value
        return normalized

    def filter_verification_status(self, queryset, name, value):
        if value in (None, ""):
            return queryset
        return queryset.filter(verification_status=self._to_status_value(value))

    def filter_verification_status_in(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            verification_status__in=[self._to_status_value(v) for v in value],
        )

    class Meta:
        model = PaymentAccount
        fields = {
            "id": ["exact"],
            "account_number": ["exact", "icontains", "istartswith"],
            "account_name": ["icontains"],
            "fsp_type": ["exact"],
            "fsp_name": ["exact", "icontains"],
            "pre_audit_status": ["exact"],
            "active_check_status": ["exact"],
            "is_primary": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }


class PaymentAccountGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    client_mutation_id = graphene.String()
    last_failure_reason = graphene.String()
    paid_at = graphene.DateTime()
    payment_in_flight = graphene.Boolean()

    def resolve_paid_at(root, info):
        from tasaf_payment.services import account_paid_at
        return account_paid_at(root.id)

    def resolve_payment_in_flight(root, info):
        from tasaf_payment.services import account_payment_in_flight
        return account_payment_in_flight(root.id)

    def resolve_last_failure_reason(root, info):
        record = (root.muse_verification_records
                  .exclude(failure_reason__isnull=True).exclude(failure_reason='')
                  .order_by('-date_created').first())
        return record.failure_reason if record else None

    class Meta:
        model = PaymentAccount
        interfaces = (graphene.relay.Node,)
        filterset_class = PaymentAccountFilterSet
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return PaymentAccount.get_queryset(queryset, info.context.user)


class MuseVerificationRecordGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = MuseVerificationRecord
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "muse_reference": ["exact", "icontains"],
            "verification_type": ["exact"],
            "result": ["exact"],
            "received_at": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "payment_account__id": ["exact"],
        }
        connection_class = ExtendedConnection


class PaylistGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    item_count = graphene.Int()
    # Outcome, derived from item statuses — answers "did this batch succeed or fail?"
    settled_count = graphene.Int()
    failed_count = graphene.Int()
    pending_count = graphene.Int()
    settled_amount = graphene.Float()
    outcome = graphene.String()
    summary = graphene.JSONString()
    last_submit = graphene.JSONString()

    class Meta:
        model = Paylist
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "batch_type": ["exact"],
            "destination": ["exact"],
            "status": ["exact"],
            "generated_at": ["exact", "lt", "lte", "gt", "gte"],
            "approved_at": ["exact", "lt", "lte", "gt", "gte"],
            "submitted_at": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "payroll__id": ["exact"],
            "payment_cycle__id": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_item_count(root, info):
        return root.items.filter(is_deleted=False).count()

    def resolve_summary(root, info):
        rows = (root.items.filter(is_deleted=False).values('status')
                .annotate(count=Count('id'), amount=Sum('amount'), net=Sum('net_amount'),
                          charge=Sum('charge_amount')))
        money = lambda v: float(v or 0)  # noqa: E731
        by_status = {r['status']: {'count': r['count'], 'amount': money(r['amount'])} for r in rows}
        return {
            'payees': sum(r['count'] for r in rows),
            'amount': sum(money(r['amount']) for r in rows),
            'net_amount': sum(money(r['net']) for r in rows),
            'charge_amount': sum(money(r['charge']) for r in rows),
            'by_status': by_status,
        }

    def resolve_last_submit(root, info):
        return (root.json_ext or {}).get('muse_last_submit')

    def resolve_settled_count(root, info):
        return root.items.filter(is_deleted=False, status=PaylistItemStatus.PROCESSED).count()

    def resolve_failed_count(root, info):
        return root.items.filter(
            is_deleted=False,
            status=PaylistItemStatus.UNAPPLIED,
        ).count()

    def resolve_pending_count(root, info):
        return root.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING).count()

    def resolve_settled_amount(root, info):
        total = root.items.filter(
            is_deleted=False, status=PaylistItemStatus.PROCESSED,
        ).aggregate(total=Sum('amount'))['total']
        return float(total or 0)

    def resolve_outcome(root, info):
        """PENDING / SUCCEEDED / FAILED / PARTIAL — never guessed from the paylist status.

        A batch is only SUCCEEDED once every item settled, and only FAILED once every
        item came back. Anything still awaiting news is PENDING, so "no news" is never
        reported as success.
        """
        counts = root.items.filter(is_deleted=False).values_list('status', flat=True)
        total = len(counts)
        if total == 0:
            return 'PENDING'
        settled = sum(1 for c in counts if c == PaylistItemStatus.PROCESSED)
        failed = sum(
            1 for c in counts
            if c == PaylistItemStatus.UNAPPLIED
        )
        if settled + failed < total:
            return 'PENDING'
        if failed == 0:
            return 'SUCCEEDED'
        if settled == 0:
            return 'FAILED'
        return 'PARTIAL'


class PaylistItemGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = PaylistItem
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "status": ["exact"],
            "muse_reference": ["exact", "icontains"],
            "is_deleted": ["exact"],
            "paylist__id": ["exact"],
            "payment_account__id": ["exact"],
        }
        connection_class = ExtendedConnection


class ReturnFeedbackGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = ReturnFeedback
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "reason_code": ["exact", "icontains"],
            "received_at": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "paylist_item__id": ["exact"],
            "paylist_item__paylist__id": ["exact"],
        }
        connection_class = ExtendedConnection


# Legacy — kept for backward compatibility (read-only, no new writes)
class VerificationRecordGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = VerificationRecord
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "match_score": ["exact", "lt", "lte", "gt", "gte"],
            "routing_decision": ["exact"],
            "run_reference": ["exact", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
        }
        connection_class = ExtendedConnection


class WithdrawalChargeGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    client_mutation_id = graphene.String()

    class Meta:
        model = WithdrawalCharge
        interfaces = (graphene.relay.Node,)
        connection_class = ExtendedConnection
        filter_fields = {
            "fsp_code": ["exact", "icontains"],
            "lower_amount": ["exact", "lte", "gte"],
            "upper_amount": ["exact", "lte", "gte"],
            "withdrawal": ["exact", "lte", "gte"],
            "is_deleted": ["exact"],
        }


class ChargeGapGQLType(graphene.ObjectType):
    range_from = graphene.String()
    range_to = graphene.String()


class FspMappingGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    client_mutation_id = graphene.String()

    class Meta:
        model = FspMapping
        interfaces = (graphene.relay.Node,)
        connection_class = ExtendedConnection
        filter_fields = {
            "fsp_name": ["exact", "icontains"],
            "fsp_code": ["exact", "icontains"],
            "is_deleted": ["exact"],
        }


class FspProviderGQLType(graphene.ObjectType):
    """One FSP on the charges page: MUSE routing data plus the names that map to it."""
    uuid = graphene.String()
    fsp_code = graphene.String()
    name = graphene.String()
    band_count = graphene.Int()
    bank_name = graphene.String()
    fsp_type = graphene.String()
    bic = graphene.String()
    names = graphene.List(graphene.String)
    has_bands = graphene.Boolean()
    accounts = graphene.Int()
    missing = graphene.List(graphene.String)
    pending = graphene.Boolean()


class MuseSettingsGQLType(graphene.ObjectType):
    institution_code = graphene.String()
    payer_account = graphene.String()
    sub_budget_class = graphene.Int()
    unapplied_sub_budget_class = graphene.Int()
    payment_desc = graphene.String()
    is_stp = graphene.Boolean()
    gl_accounts = graphene.JSONString()
    environment = graphene.String()
    date_updated = graphene.DateTime()


class MuseLogEntryGQLType(graphene.ObjectType):
    created_at = graphene.DateTime()
    direction = graphene.String()
    transaction_type = graphene.String()
    status = graphene.String()
    attempt_number = graphene.Int()
    msg_id = graphene.String()
    muse_reference = graphene.String()
    esb_request_id = graphene.String()
    http_status_code = graphene.Int()
    item_count = graphene.Int()
    amount = graphene.Float()
    benefit_code = graphene.String()
    error_message = graphene.String()
    response_body = graphene.String()


class MuseReadinessGQLType(graphene.ObjectType):
    ready = graphene.Boolean()
    settings_missing = graphene.List(graphene.String)
    providers_missing = graphene.List(graphene.String)
    server_environment = graphene.String()
    settings_environment = graphene.String()
    environment_mismatch = graphene.Boolean()
