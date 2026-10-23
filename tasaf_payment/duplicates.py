from collections import defaultdict

from django.contrib.postgres.aggregates import ArrayAgg
from django.core.cache import cache
from django.db.models import Count, F
from django.db.models.functions import Lower, Trim

from tasaf_payment import msisdn

SAME_HOUSEHOLD, SAME_ACCOUNT, SAME_PERSON = 'SAME_HOUSEHOLD', 'SAME_ACCOUNT', 'SAME_PERSON'
KINDS = (SAME_HOUSEHOLD, SAME_ACCOUNT, SAME_PERSON)
CACHE_KEY = 'tasaf.preaudit.duplicates:{}'
CACHE_SECONDS = 600


def account_key(fsp_type, number):
    number = (number or '').strip()
    if fsp_type == 'MOBILE':
        normalised = msisdn.normalise(number)
        return normalised if msisdn.is_valid(normalised) else number
    return ''.join(number.split()).upper()


def _payments(payment_cycle_id):
    from payroll.models import PayrollBenefitConsumption, PayrollStatus
    from tasaf_payment.window import household_of_benefit

    rows = (PayrollBenefitConsumption.objects
            .filter(is_deleted=False, payroll__is_deleted=False, benefit__is_deleted=False,
                    payroll__payment_cycle_id=payment_cycle_id)
            .exclude(payroll__status=PayrollStatus.REJECTED)
            .annotate(household=household_of_benefit('benefit__'))
            .values_list('benefit_id', 'benefit__code', 'benefit__amount', 'household',
                         'payroll__name', 'benefit__individual__first_name', 'benefit__individual__last_name'))
    seen, by_group = set(), defaultdict(list)
    for benefit_id, code, amount, household, payroll, first, last in rows.iterator(chunk_size=2000):
        if benefit_id in seen or not household:
            continue
        group_id = str(household)
        seen.add(benefit_id)
        by_group[group_id].append({'benefit_id': str(benefit_id), 'benefit_code': code, 'payroll': payroll,
                                   'amount': float(amount) if amount is not None else None,
                                   'head': ' '.join(p for p in (first, last) if p)})
    return by_group


def _accounts(group_ids):
    from tasaf_payment.models import PaymentAccount

    out = {}
    for group_id, fsp_type, fsp_name, number in (
            PaymentAccount.objects.filter(group_beneficiary__group_id__in=group_ids, is_primary=True,
                                          is_deleted=False)
            .order_by('date_updated')
            .values_list('group_beneficiary__group_id', 'fsp_type', 'fsp_name', 'account_number')):
        out[str(group_id)] = {'fsp_type': fsp_type, 'fsp_name': fsp_name, 'account_number': number,
                              'key': account_key(fsp_type, number)}
    return out


def _same_person(group_ids):
    from individual.models import GroupIndividual

    rows = (GroupIndividual.objects
            .filter(group_id__in=group_ids, is_deleted=False, individual__is_deleted=False,
                    individual__dob__isnull=False)
            .exclude(individual__first_name='').exclude(individual__last_name='')
            .annotate(fn=Lower(Trim(F('individual__first_name'))), ln=Lower(Trim(F('individual__last_name'))),
                      dob=F('individual__dob'))
            .values('fn', 'ln', 'dob')
            .annotate(n=Count('group_id', distinct=True), groups=ArrayAgg('group_id', distinct=True))
            .filter(n__gt=1).order_by('-n'))
    return [{'key': f"{r['fn'].title()} {r['ln'].title()}, {r['dob']}", 'groups': [str(g) for g in r['groups']]}
            for r in rows]


def window_duplicates(payment_cycle_id, refresh=False):
    from individual.models import Group

    key = CACHE_KEY.format(payment_cycle_id)
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    by_group = _payments(payment_cycle_id)
    group_ids = list(by_group)
    accounts = _accounts(group_ids)
    found = []

    for group_id, payments in by_group.items():
        if len(payments) > 1:
            found.append({'kind': SAME_HOUSEHOLD, 'key': group_id, 'groups': [group_id]})

    by_account = defaultdict(list)
    for group_id, account in accounts.items():
        if account['key']:
            by_account[account['key']].append(group_id)
    found += [{'kind': SAME_ACCOUNT, 'key': number, 'groups': groups}
              for number, groups in by_account.items() if len(groups) > 1]

    found += [{'kind': SAME_PERSON, **match} for match in _same_person(group_ids)]

    involved = {g for match in found for g in match['groups']}
    codes = {str(k): v for k, v in Group.objects.filter(id__in=involved).values_list('id', 'code')}
    groups = []
    for match in found:
        rows = []
        for group_id in match['groups']:
            account = accounts.get(group_id) or {}
            for payment in by_group.get(group_id, []):
                rows.append({**payment, 'group_id': group_id, 'hhid': codes.get(group_id, ''),
                             'account_number': account.get('account_number') or '',
                             'fsp_name': account.get('fsp_name') or ''})
        key_text = codes.get(match['key'], match['key']) if match['kind'] == SAME_HOUSEHOLD else match['key']
        groups.append({'kind': match['kind'], 'key': key_text, 'households': len(match['groups']),
                       'payments': len(rows), 'amount': sum(r['amount'] or 0 for r in rows), 'rows': rows})
    groups.sort(key=lambda g: (KINDS.index(g['kind']), -g['payments'], g['key']))

    counts = {kind: sum(1 for g in groups if g['kind'] == kind) for kind in KINDS}
    extra = {kind: sum(g['payments'] - 1 for g in groups if g['kind'] == kind) for kind in KINDS}
    report = {'households': len(by_group), 'payments': sum(len(p) for p in by_group.values()),
              'counts': counts, 'extra_payments': extra,
              'extra_amount': sum(g['amount'] - max(r['amount'] or 0 for r in g['rows']) for g in groups
                                  if g['kind'] == SAME_HOUSEHOLD and g['rows']),
              'groups': groups}
    cache.set(key, report, CACHE_SECONDS)
    return report


def duplicates_page(payment_cycle_id, kind=None, search=None, offset=0, limit=50, refresh=False):
    report = window_duplicates(payment_cycle_id, refresh=refresh)
    groups = report['groups']
    if kind:
        groups = [g for g in groups if g['kind'] == kind]
    if search:
        term = search.strip().lower()
        groups = [g for g in groups if term in g['key'].lower()
                  or any(term in (r['hhid'] or '').lower() or term in (r['head'] or '').lower()
                         or term in (r['account_number'] or '').lower() for r in g['rows'])]
    summary = {k: v for k, v in report.items() if k != 'groups'}
    return {**summary, 'total_groups': len(groups), 'groups': groups[offset:offset + limit]}
