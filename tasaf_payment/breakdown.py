"""A batch by benefit category, closing on the total sent to MUSE."""
import logging
from decimal import Decimal

from tasaf_payment.compliance import as_decimal
from tasaf_payment.muse.message import payable_items

logger = logging.getLogger(__name__)

NO_BREAKDOWN = ('NO_BREAKDOWN', 'Benefit without a breakdown')
WHOLE_BENEFIT = ('BENEFIT', 'Benefit')
CSV_COLUMNS = ('Line', 'Households', 'People', 'Amount (TZS)')


def _rules():
    try:
        from calculation.apps import CALCULATION_RULES
    except ImportError:
        return {}
    return {str(rule.uuid): rule for rule in CALCULATION_RULES}


def _split(rule, ext, amount):
    if rule is not None and hasattr(rule, 'benefit_breakdown'):
        split = rule.benefit_breakdown(ext)
        if split is not None:
            return split, False
        code, label = NO_BREAKDOWN
    else:
        code, label = WHOLE_BENEFIT
    line = {'code': code, 'label': label, 'amount': amount, 'people': 1}
    return {'lines': [line], 'components_total': amount, 'benefit_total': amount}, code == NO_BREAKDOWN[0]


def batch_breakdown(paylist):
    rules = _rules()
    plan = getattr(getattr(paylist, 'payroll', None), 'payment_plan', None)
    plan_rule = str(getattr(plan, 'calculation', '') or '')
    categories = {}
    totals = {key: Decimal('0') for key in ('components', 'cap', 'charges', 'batch')}
    capped = charged = missing = unexplained = payments = 0

    rows = payable_items(paylist).values_list(
        'amount', 'net_amount', 'charge_amount', 'benefit_consumption__amount', 'benefit_consumption__json_ext')
    for amount, net, charge, benefit_amount, ext in rows.iterator(chunk_size=5000):
        payments += 1
        amount, charge = as_decimal(amount), as_decimal(charge)
        rule = rules.get(str((ext or {}).get('calculation_rule_uuid') or plan_rule))
        whole = as_decimal(benefit_amount if benefit_amount is not None else net if net is not None else amount)
        split, no_breakdown = _split(rule, ext, whole)
        missing += no_breakdown

        for line in split['lines']:
            row = categories.setdefault(line['code'], {
                'code': line['code'], 'label': line['label'], 'amount': Decimal('0'), 'households': 0, 'people': 0})
            row['amount'] += line['amount']
            row['households'] += int(line['amount'] != 0)
            row['people'] += line['people']
        lines_total = sum((line['amount'] for line in split['lines']), Decimal('0'))
        reduction = split['components_total'] - split['benefit_total']
        totals['components'] += lines_total
        totals['cap'] += reduction
        capped += int(reduction != 0)
        totals['charges'] += charge
        charged += int(charge != 0)
        totals['batch'] += amount
        unexplained += int(amount != lines_total - reduction + charge)

    totals['benefits'] = totals['components'] - totals['cap']
    difference = totals['batch'] - (totals['benefits'] + totals['charges'])
    problems = []
    if missing:
        problems.append(f'{missing} payment(s) have no benefit breakdown from their calculation rule')
    if unexplained:
        problems.append(f'{unexplained} payment(s) do not match their calculated benefit '
                        f'(the amounts differ by TZS {difference:,.0f} in total)')
    return {
        'reference': paylist.muse_batch_reference or str(paylist.uuid),
        'lines': [{**row, 'amount': float(row['amount'])} for row in categories.values()],
        'components_total': float(totals['components']),
        'cap_reduction': {'amount': float(totals['cap']), 'households': capped},
        'benefits_total': float(totals['benefits']),
        'charges': {'amount': float(totals['charges']), 'payments': charged},
        'unexplained': {'amount': float(difference), 'payments': unexplained},
        'batch_total': float(totals['batch']),
        'payments': payments,
        'blocked': bool(problems),
        'problems': problems,
    }


def refusal(report):
    if not report['blocked']:
        return None
    return 'The benefit breakdown does not add up to the batch total, so it cannot be sent: ' + \
        '; '.join(report['problems'])


def _number(value):
    return int(value) if value == int(value) else round(value, 2)


def csv_rows(report):
    rows = [(line['label'], line['households'], line['people'], line['amount']) for line in report['lines']]
    rows += [
        ('Components total', '', '', report['components_total']),
        ('Household cap reduction', report['cap_reduction']['households'], '', -report['cap_reduction']['amount']),
        ('Benefits', report['payments'], '', report['benefits_total']),
        ('Withdrawal charges', report['charges']['payments'], '', report['charges']['amount']),
        ('Unexplained difference', report['unexplained']['payments'], '', report['unexplained']['amount']),
        (f"Batch total sent to MUSE ({report['reference']})", report['payments'], '', report['batch_total']),
    ]
    return [(*row[:3], _number(row[3])) for row in rows]


def export_breakdown(user, paylist):
    import uuid

    from django.core.files.base import ContentFile
    from pandas import DataFrame
    from core.models import ExportableQueryModel

    report = batch_breakdown(paylist)
    frame = DataFrame.from_records(csv_rows(report), columns=CSV_COLUMNS)
    filename = f"{uuid.uuid4()}.csv"
    export = ExportableQueryModel(
        name=filename, model='Paylist', content=ContentFile(frame.to_csv(index=False), filename), user=user,
        sql_query='tasaf_payment.breakdown.export_breakdown', file_format=ExportableQueryModel.FileFormat.CSV)
    export.save()
    logger.info("Benefit breakdown export for %s: %s", report['reference'], filename)
    return export.name
