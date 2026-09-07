import django_filters
import graphene
from django.db.models import Sum
from graphene_django import DjangoObjectType
from graphene_django.filter import TypedFilter

from core import prefix_filterset, ExtendedConnection
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

    def resolve_settled_count(root, info):
        return root.items.filter(is_deleted=False, status=PaylistItemStatus.PROCESSED).count()

    def resolve_failed_count(root, info):
        return root.items.filter(
            is_deleted=False,
            status__in=[PaylistItemStatus.RETURNED, PaylistItemStatus.UNAPPLIED],
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
            if c in (PaylistItemStatus.RETURNED, PaylistItemStatus.UNAPPLIED)
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
            "final_status": ["exact"],
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
            "feedback_type": ["exact"],
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


class FspCoverageGQLType(graphene.ObjectType):
    """Configured vs unconfigured ranges, so gaps are filled deliberately rather than
    discovered when a payment underpays."""
    fsp_code = graphene.String()
    bands = graphene.Int()
    lowest = graphene.String()
    highest = graphene.String()
    covers_from_zero = graphene.Boolean()
    gaps = graphene.List(ChargeGapGQLType)
    overlaps = graphene.List(ChargeGapGQLType)


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


class UnmappedFspGQLType(graphene.ObjectType):
    """An FSP present on payment accounts that has no configured tariff bands."""
    fsp_name = graphene.String()
    resolved_code = graphene.String()


class KnownFspGQLType(graphene.ObjectType):
    """An FSP the UI can offer: seen on payment accounts, or already holding tariff bands."""
    fsp_code = graphene.String()
    fsp_name = graphene.String()
    on_accounts = graphene.Boolean()
    has_bands = graphene.Boolean()
