import graphene
from graphene_django import DjangoObjectType

from core import prefix_filterset, ExtendedConnection
from tasaf_payment.models import (
    PaymentAccount,
    VerificationRecord,
    MuseVerificationRecord,
    Paylist,
    PaylistItem,
    ReturnFeedback,
)


class PaymentAccountGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    client_mutation_id = graphene.String()

    class Meta:
        model = PaymentAccount
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "account_number": ["exact", "icontains", "istartswith"],
            "account_name": ["icontains"],
            "fsp_type": ["exact"],
            "fsp_name": ["exact", "icontains"],
            "verification_status": ["exact"],
            "pre_audit_status": ["exact"],
            "active_check_status": ["exact"],
            "is_primary": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
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

    class Meta:
        model = Paylist
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "batch_type": ["exact"],
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
