from decimal import Decimal

from django.db.models import (
    Case, CharField, Count, IntegerField, OuterRef, Subquery, Sum, UUIDField, Value, When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce

CHANNELS = {'MOBILE': 'MNO', 'BANK': 'BANK'}
NO_ACCOUNT = 'NO_ACCOUNT'
NO_HOUSEHOLD = 'NO_HOUSEHOLD'
BUCKETS = ('MNO', 'BANK', NO_ACCOUNT, NO_HOUSEHOLD)


def household_of_benefit(prefix=''):
    from individual.models import GroupIndividual

    payee_household = (GroupIndividual.objects
                       .filter(individual_id=OuterRef(f'{prefix}individual_id'), is_deleted=False,
                               group__is_deleted=False)
                       .order_by(Case(When(role=GroupIndividual.Role.HEAD, then=0), default=1,
                                      output_field=IntegerField()), 'date_created')
                       .values('group_id')[:1])
    return Coalesce(Cast(KeyTextTransform('beneficiary_group_id', f'{prefix}json_ext'), UUIDField()),
                    Subquery(payee_household), output_field=UUIDField())


def window_benefits(payment_cycle_id):
    from payroll.models import BenefitConsumption, PayrollBenefitConsumption, PayrollStatus

    benefit_ids = PayrollBenefitConsumption.objects.filter(
        is_deleted=False, payroll__is_deleted=False, payroll__payment_cycle_id=payment_cycle_id,
    ).exclude(payroll__status=PayrollStatus.REJECTED).values('benefit_id')
    return BenefitConsumption.objects.filter(id__in=benefit_ids, is_deleted=False).annotate(
        group_id=household_of_benefit())


def _with_channel(benefits):
    from tasaf_payment.models import PaymentAccount

    fsp_type = PaymentAccount.objects.filter(
        group_beneficiary__group_id=OuterRef('group_id'), is_primary=True, is_deleted=False,
    ).order_by('-date_updated').values('fsp_type')[:1]
    return benefits.annotate(fsp_type=Coalesce(Subquery(fsp_type), Value(''), output_field=CharField()))


def window_overview(payment_cycle_id):
    totals = {key: {'households': 0, 'benefits': 0, 'amount': Decimal('0')} for key in BUCKETS}
    rows = (_with_channel(window_benefits(payment_cycle_id))
            .annotate(no_household=Case(When(group_id__isnull=True, then=1), default=0,
                                        output_field=IntegerField()))
            .values('fsp_type', 'no_household')
            .annotate(households=Count('group_id', distinct=True), benefits=Count('id'), amount=Sum('amount')))
    for row in rows:
        key = NO_HOUSEHOLD if row['no_household'] else CHANNELS.get(row['fsp_type'], NO_ACCOUNT)
        bucket = totals[key]
        bucket['households'] += row['households']
        bucket['benefits'] += row['benefits']
        bucket['amount'] += row['amount'] or Decimal('0')
    totals['TOTAL'] = {key: sum(totals[c][key] for c in BUCKETS) for key in ('households', 'benefits', 'amount')}
    return {channel: {**values, 'amount': float(values['amount'])} for channel, values in totals.items()}
