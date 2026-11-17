import pickle
import zlib
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


def cache_get(key):
    from django.core.cache import cache
    blob = cache.get(key)
    if not isinstance(blob, bytes):
        return None
    try:
        return pickle.loads(zlib.decompress(blob))
    except (zlib.error, pickle.UnpicklingError, EOFError, AttributeError, ImportError):
        return None


def cache_set(key, value, seconds):
    from django.core.cache import cache
    cache.set(key, zlib.compress(pickle.dumps(value, pickle.HIGHEST_PROTOCOL)), seconds)


def cache_generation(report, payment_cycle_id, bump=False):
    from django.core.cache import cache
    key = f'tasaf.preaudit.{report}.generation:{payment_cycle_id}'
    generation = cache.get(key) or 0
    if bump:
        generation += 1
        cache.set(key, generation, None)
    return generation


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


def households_in(location_id):
    from individual.models import Group
    from tasaf_payment.services import location_descendants_q

    return Group.objects.filter(location_descendants_q(location_id, base='location'), is_deleted=False).values('id')


def in_location(queryset, location_id, field='group_id'):
    if not location_id:
        return queryset
    return queryset.filter(**{f'{field}__in': households_in(location_id)})


def window_payments(payment_cycle_id, location_id=None):
    from payroll.models import PayrollBenefitConsumption, PayrollStatus

    links = (PayrollBenefitConsumption.objects
             .filter(is_deleted=False, payroll__is_deleted=False, benefit__is_deleted=False,
                     payroll__payment_cycle_id=payment_cycle_id)
             .exclude(payroll__status=PayrollStatus.REJECTED)
             .annotate(household=household_of_benefit('benefit__')))
    return in_location(links, location_id, field='household')


def window_benefits(payment_cycle_id, location_id=None):
    from payroll.models import BenefitConsumption, PayrollBenefitConsumption, PayrollStatus

    benefit_ids = PayrollBenefitConsumption.objects.filter(
        is_deleted=False, payroll__is_deleted=False, payroll__payment_cycle_id=payment_cycle_id,
    ).exclude(payroll__status=PayrollStatus.REJECTED).values('benefit_id')
    benefits = BenefitConsumption.objects.filter(id__in=benefit_ids, is_deleted=False).annotate(
        group_id=household_of_benefit())
    return in_location(benefits, location_id)


def _with_channel(benefits):
    from tasaf_payment.models import PaymentAccount

    fsp_type = PaymentAccount.objects.filter(
        group_beneficiary__group_id=OuterRef('group_id'), is_primary=True, is_deleted=False,
    ).order_by('-date_updated').values('fsp_type')[:1]
    return benefits.annotate(fsp_type=Coalesce(Subquery(fsp_type), Value(''), output_field=CharField()))


def window_overview(payment_cycle_id, location_id=None):
    totals = {key: {'households': 0, 'benefits': 0, 'amount': Decimal('0')} for key in BUCKETS}
    rows = (_with_channel(window_benefits(payment_cycle_id, location_id))
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
