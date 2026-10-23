from collections import defaultdict
from decimal import Decimal

from django.core.cache import cache

MATCH, OVERPAID, UNDERPAID, DATA_ERROR = 'MATCH', 'OVERPAID', 'UNDERPAID', 'DATA_ERROR'
STATUSES = (MATCH, OVERPAID, UNDERPAID, DATA_ERROR)
COMPONENTS = ('base', 'disability', 'health', 'primary', 'secondary')
TOLERANCE = Decimal('0.5')
CACHE_KEY = 'tasaf.preaudit.compliance:{}'
CACHE_SECONDS = 600


def _dec(value):
    try:
        return Decimal(str(value)) if value not in (None, '') else Decimal('0')
    except Exception:
        return Decimal('0')


def _count(breakdown, key):
    raw = breakdown.get(f'{key}_raw')
    return int(_dec(raw if raw is not None else breakdown.get(key)))


def rule_params(payment_plan):
    from calcrule_pct_payment.calculation_rule import PctPaymentCalculationRule

    if payment_plan is None or str(payment_plan.calculation) != PctPaymentCalculationRule.uuid:
        return None
    return PctPaymentCalculationRule._get_rule_params(payment_plan)


def expected(breakdown, params):
    disabled = bool(breakdown.get('has_disability')) or _dec(breakdown.get('disability_amount')) > 0
    children = _count(breakdown, 'young_child_count')
    primary = _count(breakdown, 'primary_count')
    secondary = _count(breakdown, 'secondary_count')
    parts = {
        'base': params['base_amount'],
        'disability': params['disability_top_up'] if disabled else Decimal('0'),
        'health': params['young_child_unit'] * min(children, params['young_child_cap_count']),
        'primary': params['primary_unit'] * min(primary, params['primary_cap_count']),
        'secondary': params['secondary_unit'] * min(secondary, params['secondary_cap_count']),
    }
    raw_total = sum(parts.values())
    capped = min(raw_total, params['household_cap_amount'])
    penalties = sum((_dec(v) for k, v in breakdown.items() if k.endswith('_penalty') or k == 'overpayment'),
                    Decimal('0'))
    return {'parts': parts, 'raw_total': raw_total, 'cap_cut': raw_total - capped, 'penalties': penalties,
            'total': capped - penalties, 'children': min(children, params['young_child_cap_count']),
            'disabled': disabled, 'primary_count': min(primary, params['primary_cap_count']),
            'secondary_count': min(secondary, params['secondary_cap_count'])}


PAID_KEYS = {'base': 'base_amount', 'disability': 'disability_amount', 'health': 'young_child_amount',
             'primary': 'primary_amount', 'secondary': 'secondary_amount'}


def assess(breakdown, amount, params):
    if params is None:
        return {'status': DATA_ERROR, 'reason': 'NOT_PCT_RULE'}
    if not breakdown:
        return {'status': DATA_ERROR, 'reason': 'NO_BREAKDOWN'}
    if amount is None:
        return {'status': DATA_ERROR, 'reason': 'NO_AMOUNT'}
    exp = expected(breakdown, params)
    paid = _dec(amount)
    variance = paid - exp['total']
    if abs(variance) <= TOLERANCE:
        status = MATCH
    else:
        status = OVERPAID if variance > 0 else UNDERPAID
    differs = [c for c in COMPONENTS if c != 'base' and PAID_KEYS[c] in breakdown
               and abs(_dec(breakdown.get(PAID_KEYS[c])) - exp['parts'][c]) > TOLERANCE]
    return {'status': status, 'reason': ','.join(differs), 'expected': exp, 'paid': paid, 'variance': variance}


def _rows(payment_cycle_id):
    from contribution_plan.models import PaymentPlan
    from individual.models import Group
    from payroll.models import PayrollBenefitConsumption, PayrollStatus
    from tasaf_payment.window import household_of_benefit

    pbc = (PayrollBenefitConsumption.objects
           .filter(is_deleted=False, payroll__is_deleted=False, benefit__is_deleted=False,
                   payroll__payment_cycle_id=payment_cycle_id)
           .exclude(payroll__status=PayrollStatus.REJECTED)
           .annotate(household=household_of_benefit('benefit__'))
           .values_list('benefit_id', 'benefit__code', 'benefit__amount', 'benefit__json_ext',
                        'payroll__payment_plan_id', 'payroll__name',
                        'benefit__individual__first_name', 'benefit__individual__last_name', 'household'))
    seen, rows, plans = set(), [], {}
    for benefit_id, code, amount, ext, plan_id, payroll, first, last, household in pbc.iterator(chunk_size=2000):
        if benefit_id in seen:
            continue
        seen.add(benefit_id)
        if plan_id not in plans:
            plans[plan_id] = rule_params(PaymentPlan.objects.filter(id=plan_id).first()) if plan_id else None
        ext = ext or {}
        rows.append((benefit_id, code, amount, ext.get('pct_breakdown') or {}, plans[plan_id], payroll,
                     str(household) if household else None, ' '.join(p for p in (first, last) if p)))
    codes = dict(Group.objects.filter(id__in={r[6] for r in rows if r[6]}).values_list('id', 'code'))
    codes = {str(k): v for k, v in codes.items()}
    return rows, codes


def window_compliance(payment_cycle_id, refresh=False):
    key = CACHE_KEY.format(payment_cycle_id)
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    rows, codes = _rows(payment_cycle_id)
    counts = {s: 0 for s in STATUSES}
    variance = {OVERPAID: Decimal('0'), UNDERPAID: Decimal('0')}
    totals = defaultdict(Decimal)
    receiving = defaultdict(int)
    findings = []
    for benefit_id, code, amount, breakdown, params, payroll, group_id, head in rows:
        result = assess(breakdown, amount, params)
        status = result['status']
        counts[status] += 1
        exp = result.get('expected')
        if exp:
            for part in COMPONENTS:
                totals[part] += exp['parts'][part]
            totals['cap_cut'] += exp['cap_cut']
            totals['penalties'] += exp['penalties']
            totals['expected'] += exp['total']
            totals['paid'] += result['paid']
            receiving['disability'] += int(exp['disabled'])
            receiving['health'] += int(exp['children'] > 0)
            receiving['children'] += exp['children']
            receiving['primary'] += int(exp['primary_count'] > 0)
            receiving['primary_students'] += exp['primary_count']
            receiving['secondary'] += int(exp['secondary_count'] > 0)
            receiving['secondary_students'] += exp['secondary_count']
            receiving['capped'] += int(exp['cap_cut'] > 0)
        if status in variance:
            variance[status] += result['variance']
        if status != MATCH:
            findings.append({
                'benefit_id': str(benefit_id), 'benefit_code': code, 'hhid': codes.get(str(group_id), ''),
                'group_id': group_id, 'head': head, 'payroll': payroll, 'status': status,
                'reason': result.get('reason', ''),
                'expected': float(exp['total']) if exp else None,
                'paid': float(result['paid']) if exp else (float(amount) if amount is not None else None),
                'variance': float(result['variance']) if exp else None,
            })
    findings.sort(key=lambda f: (STATUSES.index(f['status']), -abs(f['variance'] or 0)))
    report = {
        'payments': len(rows),
        'counts': counts,
        'variance': {k: float(v) for k, v in variance.items()},
        'totals': {k: float(v) for k, v in totals.items()},
        'receiving': dict(receiving),
        'findings': findings,
    }
    cache.set(key, report, CACHE_SECONDS)
    return report


def compliance_page(payment_cycle_id, status=None, search=None, offset=0, limit=50, refresh=False):
    report = window_compliance(payment_cycle_id, refresh=refresh)
    findings = report['findings']
    if status:
        findings = [f for f in findings if f['status'] == status]
    if search:
        term = search.strip().lower()
        findings = [f for f in findings if term in (f['hhid'] or '').lower() or term in (f['head'] or '').lower()
                    or term in (f['benefit_code'] or '').lower()]
    summary = {k: v for k, v in report.items() if k != 'findings'}
    return {**summary, 'total_findings': len(findings), 'findings': findings[offset:offset + limit]}


EXPORT_COLUMNS = ['HHID', 'Head of household', 'Payroll', 'Payment code', 'Base', 'Disability',
                  'Children 0-5', 'Health', 'Primary students', 'Primary', 'Secondary students', 'Secondary',
                  'Cap cut', 'Penalties', 'Expected', 'Paid', 'Variance', 'Status', 'Differs in']


def export_compliance(user, payment_cycle_id, status=None):
    import uuid as _uuid

    from django.core.files.base import ContentFile
    from pandas import DataFrame
    from core.models import ExportableQueryModel

    rows, codes = _rows(payment_cycle_id)
    records = []
    for benefit_id, code, amount, breakdown, params, payroll, group_id, head in rows:
        result = assess(breakdown, amount, params)
        if status and result['status'] != status:
            continue
        exp = result.get('expected')
        parts = exp['parts'] if exp else {}
        records.append([
            codes.get(str(group_id), ''), head, payroll, code,
            float(parts.get('base', 0)), float(parts.get('disability', 0)),
            exp['children'] if exp else None, float(parts.get('health', 0)),
            exp['primary_count'] if exp else None, float(parts.get('primary', 0)),
            exp['secondary_count'] if exp else None, float(parts.get('secondary', 0)),
            float(exp['cap_cut']) if exp else None, float(exp['penalties']) if exp else None,
            float(exp['total']) if exp else None, float(amount) if amount is not None else None,
            float(result['variance']) if exp else None, result['status'], result.get('reason', ''),
        ])
    frame = DataFrame.from_records(records, columns=EXPORT_COLUMNS)
    filename = f"{_uuid.uuid4()}.csv"
    ExportableQueryModel(
        name=filename, model='PreAuditCompliance', content=ContentFile(frame.to_csv(index=False), filename),
        user=user, sql_query='aggregation: tasaf_payment.compliance.export_compliance',
        file_format=ExportableQueryModel.FileFormat.CSV,
    ).save()
    return filename
