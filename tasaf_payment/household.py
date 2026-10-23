from types import SimpleNamespace

from tasaf_payment.compliance import COMPONENTS, PAID_KEYS, TOLERANCE, _dec, assess, expected, rule_params

STILL_TRUE, CHANGED = 'STILL_TRUE', 'CHANGED'
MAX_MATCHES = 10


def _num(value):
    return float(value) if value is not None else None


def _members(group, params, reference_date):
    from calcrule_pct_payment.calculation_rule import PctPaymentCalculationRule as Rule

    links = (group.groupindividuals.filter(is_deleted=False, individual__is_deleted=False)
             .select_related('individual').order_by('individual__dob'))
    members = []
    for link in links:
        person = link.individual
        age = Rule._age_years(person.dob, reference_date=reference_date)
        child = bool(params) and Rule._is_age_in_range(
            person.dob, params['young_child_age_min'], params['young_child_age_max'], reference_date)
        members.append({
            'name': ' '.join(p for p in (person.first_name, person.last_name) if p),
            'dob': str(person.dob) if person.dob else None, 'age': age,
            'role': link.role or '', 'recipient': link.recipient_type or '', 'active': link.is_active,
            'child': child, 'disabled': Rule._member_has_disability(person),
            'primary': Rule._get_member_value(person, params['primary_count_field']) if params else None,
            'secondary': Rule._get_member_value(person, params['secondary_count_field']) if params else None,
        })
    return members


def _recheck(group, params, reference_date, recorded):
    from calcrule_pct_payment.calculation_rule import PctPaymentCalculationRule as Rule

    current = Rule._calculate_household_breakdown(SimpleNamespace(group=group), params,
                                                  reference_date=reference_date)
    now, then = expected(current, params), expected(recorded, params)
    changed = [c for c in COMPONENTS if abs(now['parts'][c] - then['parts'][c]) > TOLERANCE]
    return {'status': CHANGED if changed else STILL_TRUE, 'changed': changed,
            'parts': {c: _num(now['parts'][c]) for c in COMPONENTS},
            'children': now['children'], 'disabled': now['disabled'],
            'primary_count': now['primary_count'], 'secondary_count': now['secondary_count'],
            'cap_cut': _num(now['cap_cut']), 'total': _num(now['total'])}


def _payment(benefit, payroll, params, group, reference_date):
    breakdown = (benefit.json_ext or {}).get('pct_breakdown') or {}
    result = assess(breakdown, benefit.amount, params)
    exp = result.get('expected')
    parts = []
    if exp:
        for c in COMPONENTS:
            recorded = breakdown.get(PAID_KEYS[c])
            parts.append({'part': c, 'expected': _num(exp['parts'][c]),
                          'recorded': _num(_dec(recorded)) if recorded is not None else None})
    return {
        'benefit_id': str(benefit.id), 'benefit_code': benefit.code, 'payroll': payroll.name,
        'payroll_status': payroll.status, 'benefit_status': benefit.status,
        'amount': _num(benefit.amount), 'status': result['status'], 'reason': result.get('reason', ''),
        'parts': parts,
        'counts': {'children': exp['children'], 'disabled': exp['disabled'],
                   'primary': exp['primary_count'], 'secondary': exp['secondary_count']} if exp else None,
        'cap_cut': _num(exp['cap_cut']) if exp else None, 'penalties': _num(exp['penalties']) if exp else None,
        'expected': _num(exp['total']) if exp else None,
        'variance': _num(result['variance']) if exp else None,
        'recheck': _recheck(group, params, reference_date, breakdown) if exp else None,
    }


def _history(group, member_ids):
    from payroll.models import PayrollBenefitConsumption
    from tasaf_payment.models import PaylistItem
    from tasaf_payment.window import household_of_benefit

    links = (PayrollBenefitConsumption.objects
             .filter(is_deleted=False, benefit__is_deleted=False, benefit__individual_id__in=member_ids)
             .annotate(household=household_of_benefit('benefit__'))
             .filter(household=group.id)
             .select_related('benefit', 'payroll', 'payroll__payment_cycle')
             .order_by('-payroll__payment_cycle__start_date', 'payroll__name'))
    rows, seen = [], set()
    for link in links:
        benefit = link.benefit
        if benefit.id in seen:
            continue
        seen.add(benefit.id)
        cycle = link.payroll.payment_cycle
        rows.append({'benefit_id': str(benefit.id), 'benefit_code': benefit.code,
                     'cycle': cycle.code if cycle else '', 'cycle_id': str(cycle.id) if cycle else None,
                     'start_date': str(cycle.start_date) if cycle else None,
                     'payroll': link.payroll.name, 'payroll_status': link.payroll.status,
                     'amount': _num(benefit.amount), 'paylist_status': ''})
    items = (PaylistItem.objects.filter(benefit_consumption_id__in=seen, is_deleted=False)
             .order_by('date_updated').values_list('benefit_consumption_id', 'status'))
    paid = {str(b): s for b, s in items}
    for row in rows:
        row['paylist_status'] = paid.get(row['benefit_id'], '')
    return rows


def _account(group_id):
    from tasaf_payment.models import PaymentAccount, VerificationStatus

    account = (PaymentAccount.objects.filter(group_beneficiary__group_id=group_id, is_primary=True,
                                             is_deleted=False).order_by('-date_updated').first())
    if not account:
        return None
    return {'fsp_type': account.fsp_type, 'fsp_name': account.fsp_name,
            'account_number': account.account_number, 'account_name': account.account_name or '',
            'verification': VerificationStatus(account.verification_status).label,
            'pre_audit': account.pre_audit_status}


def _duplicates(payment_cycle_id, group_id):
    from tasaf_payment.duplicates import window_duplicates

    return [{'kind': g['kind'], 'key': g['key'], 'households': g['households'], 'payments': g['payments']}
            for g in window_duplicates(payment_cycle_id)['groups']
            if any(r['group_id'] == group_id for r in g['rows'])]


def household_detail(payment_cycle_id, group_id=None, hhid=None):
    from contribution_plan.models import PaymentPlan
    from individual.models import Group
    from payment_cycle.models import PaymentCycle
    from payroll.models import PayrollStatus

    from tasaf_payment.window import window_benefits

    cycle = PaymentCycle.objects.filter(id=payment_cycle_id).first()
    groups = Group.objects.filter(is_deleted=False)
    if group_id:
        group = groups.filter(id=group_id).first()
    else:
        hhid = (hhid or '').strip()
        group = groups.filter(code__iexact=hhid).first() if hhid else None
        if hhid and not group:
            found = list(groups.filter(code__icontains=hhid).order_by('code').values_list('code', flat=True)[:MAX_MATCHES + 1])
            if len(found) > 1:
                return {'matches': found[:MAX_MATCHES], 'more': len(found) > MAX_MATCHES}
            group = groups.filter(code=found[0]).first() if found else None
    if not group or not cycle:
        return None
    reference_date = cycle.end_date or cycle.start_date
    group_id = str(group.id)

    payments, plans = [], {}
    benefits = window_benefits(payment_cycle_id).filter(group_id=group.id)
    for benefit in benefits.order_by('date_created'):
        link = (benefit.payrollbenefitconsumption_set
                .filter(is_deleted=False, payroll__payment_cycle_id=payment_cycle_id)
                .exclude(payroll__status=PayrollStatus.REJECTED).select_related('payroll').first())
        plan_id = link.payroll.payment_plan_id
        if plan_id not in plans:
            plans[plan_id] = rule_params(PaymentPlan.objects.filter(id=plan_id).first()) if plan_id else None
        payments.append(_payment(benefit, link.payroll, plans[plan_id], group, reference_date))

    params = next((p for p in plans.values() if p), None)
    members = _members(group, params, reference_date)
    head = next((m['name'] for m in members if m['role'] == 'HEAD'), '')
    location = group.location
    return {
        'group_id': group_id, 'hhid': group.code, 'head': head,
        'location': f'{location.name} ({location.code})' if location else '',
        'cycle': cycle.code, 'reference_date': str(reference_date),
        'account': _account(group.id), 'members': members, 'payments': payments,
        'duplicates': _duplicates(payment_cycle_id, group_id),
        'history': _history(group, group.groupindividuals.filter(is_deleted=False).values('individual_id')),
    }
