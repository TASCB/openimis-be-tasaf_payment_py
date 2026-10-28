"""Aggregations for the auditor Reports tab.

Counts only PROCESSED items: "paid" means the gateway confirmed it. Component amounts
come from BenefitConsumption.json_ext['pct_breakdown'], written by calcrule_pct_payment.

Components are PRE-CAP -- the rule caps a household total, so they can sum to more than
the money that moved. componentTotal and totalPaid are both reported and are not
expected to match when the cap bites.
"""
import logging
from collections import defaultdict
from decimal import Decimal

import pandas as pd

from tasaf_payment.charges import resolve_fsp_code
from tasaf_payment.models import PaylistItem, PaylistItemStatus

logger = logging.getLogger(__name__)

def fsp_names_for_code(code):
    """Every stored fsp_name that resolves to this EPAYMENT_CODE.

    The code is not a column — it comes from FspMapping, then config aliases, then
    the normalised name (see charges.resolve_fsp_code). So drilling into a FSP means
    resolving the distinct names and keeping the ones that land on the wanted code.
    Two spellings can share a code ("Vodacom M-Pesa" and "M-Pesa" both map to MPESA),
    which is exactly why this cannot be a simple equality filter.
    """
    from tasaf_payment.models import PaymentAccount

    wanted = (code or '').strip().upper()
    names = (
        PaymentAccount.objects.filter(is_deleted=False)
        .exclude(fsp_name__isnull=True).exclude(fsp_name='')
        .values_list('fsp_name', flat=True).distinct()
    )
    return [name for name in names if (resolve_fsp_code(name) or '').upper() == wanted]


def _dec(value):
    """jsonb numbers arrive as int/float/str/None — normalise to Decimal."""
    if value in (None, ''):
        return Decimal('0')
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return Decimal('0')


def _blank_row(code):
    return {
        'epayment_code': code,
        'households': set(),  # collapsed to a count before returning
        'withdrawal_charges': Decimal('0'),
        'pct_payment': Decimal('0'),
        'child_grant': Decimal('0'),
        'disability_grant': Decimal('0'),
        'ei_payment': Decimal('0'),
        'pwp_payment': Decimal('0'),
        'component_total': Decimal('0'),
        'total_paid': Decimal('0'),
        'has_child': 0,
        'primary_student': 0,
        'secondary_student': 0,
        'items': 0,
    }


def epayment_summary_by_fsp(payment_cycle_id=None, date_from=None, date_to=None,
                            destination=None):
    """
    Build the "Summary of e-Payment by FSP" rows plus a totals row.

    Filters are all optional and AND-ed. ``date_from`` / ``date_to`` apply to
    ``PaylistItem.settled_at`` — the date the money actually landed, not the date the batch
    was generated, which is the date an auditor means.
    """
    qs = (
        PaylistItem.objects
        .filter(is_deleted=False, status=PaylistItemStatus.PROCESSED)
        .select_related('payment_account', 'benefit_consumption')
    )
    if payment_cycle_id:
        qs = qs.filter(paylist__payment_cycle_id=payment_cycle_id)
    if destination:
        qs = qs.filter(paylist__destination=destination)
    if date_from:
        qs = qs.filter(settled_at__gte=date_from)
    if date_to:
        qs = qs.filter(settled_at__lte=date_to)

    # resolve_fsp_code hits the DB, so cache per distinct name rather than per row.
    code_cache = {}

    rows = defaultdict(lambda: None)

    for item in qs.iterator(chunk_size=2000):
        account = item.payment_account
        fsp_name = getattr(account, 'fsp_name', None)
        if fsp_name not in code_cache:
            code_cache[fsp_name] = resolve_fsp_code(fsp_name)
        code = code_cache[fsp_name] or 'UNMAPPED'

        row = rows[code]
        if row is None:
            row = rows[code] = _blank_row(code)

        row['items'] += 1
        row['withdrawal_charges'] += _dec(item.charge_amount)
        # Entitlement, not the grossed-up transfer -- what beneficiaries received.
        row['total_paid'] += _dec(item.net_amount if item.net_amount is not None else item.amount)

        benefit = item.benefit_consumption
        ext = (getattr(benefit, 'json_ext', None) or {}) if benefit else {}
        breakdown = ext.get('pct_breakdown') or {}

        group_id = ext.get('beneficiary_group_id') or getattr(
            getattr(account, 'group_beneficiary', None), 'group_id', None,
        )
        if group_id:
            row['households'].add(str(group_id))

        if not breakdown:
            continue

        row['pct_payment'] += _dec(breakdown.get('base_amount'))
        row['child_grant'] += _dec(breakdown.get('young_child_amount'))
        row['disability_grant'] += _dec(breakdown.get('disability_amount'))
        row['component_total'] += _dec(breakdown.get('raw_total'))

        if int(_dec(breakdown.get('young_child_count'))) > 0:
            row['has_child'] += 1
        row['primary_student'] += int(_dec(breakdown.get('primary_count')))
        row['secondary_student'] += int(_dec(breakdown.get('secondary_count')))

    out = []
    for code in sorted(rows):
        row = dict(rows[code])
        row['households'] = len(row['households'])
        out.append(row)

    totals = _blank_row('TOTAL')
    totals['households'] = sum(r['households'] for r in out)
    for key in ('withdrawal_charges', 'pct_payment', 'child_grant', 'disability_grant',
                'ei_payment', 'pwp_payment', 'component_total', 'total_paid'):
        totals[key] = sum((r[key] for r in out), Decimal('0'))
    for key in ('has_child', 'primary_student', 'secondary_student', 'items'):
        totals[key] = sum(r[key] for r in out)

    logger.info(
        "epayment_summary_by_fsp: %d FSP row(s), %d item(s), %s households",
        len(out), totals['items'], totals['households'],
    )
    return {'rows': out, 'totals': totals}


# CSV, as core does it.

EXPORT_COLUMNS = [
    ('epayment_code', 'EPAYMENT_CODE'),
    ('households', 'HOUSEHOLDS'),
    ('withdrawal_charges', 'WITHDRAWAL_CHARGES'),
    ('pct_payment', 'PCT_PAYMENT'),
    ('child_grant', 'CHILD_GRANT'),
    ('disability_grant', 'DISABILITY_GRANT'),
    ('pwp_payment', 'PWP_PAYMENT'),
    ('ei_payment', 'EI_PAYMENT'),
    ('has_child', 'Has Child'),
    ('primary_student', 'Primary Student'),
    ('secondary_student', 'Secondary Student'),
    ('total_paid', 'TOTAL_PAID'),
]


def export_epayment_summary_by_fsp(user, **filters):
    """Write the summary as CSV and return the export name for core's fetch_export."""
    import uuid as _uuid

    from django.core.files.base import ContentFile
    from pandas import DataFrame
    from core.models import ExportableQueryModel

    result = epayment_summary_by_fsp(**filters)

    # float keeps the values numeric for anything re-reading the file.
    def cell(value):
        return float(value) if isinstance(value, Decimal) else value

    records = [
        {label: cell(row[key]) for key, label in EXPORT_COLUMNS}
        for row in result['rows'] + [result['totals']]
    ]
    frame = DataFrame.from_records(records, columns=[label for _, label in EXPORT_COLUMNS])

    filename = f"{_uuid.uuid4()}.csv"
    export = ExportableQueryModel(
        name=filename,
        model='EpaymentSummaryByFsp',
        content=ContentFile(frame.to_csv(index=False), filename),
        user=user,
        sql_query='aggregation: tasaf_payment.reports.epayment_summary_by_fsp',
        file_format=ExportableQueryModel.FileFormat.CSV,
    )
    export.save()
    logger.info("export_epayment_summary_by_fsp: %s (%d row(s))", filename, len(records))
    return export.name


PAYLIST_ITEM_EXPORT_COLUMNS = [
    'HHID', 'Payee Code', 'Payee', 'Payment Ref', 'Account Number', 'Account Name', 'FSP', 'FSP Type',
    'Region', 'District', 'Ward', 'Village',
    'Gross Amount', 'Net Amount', 'Charge', 'Status', 'MUSE Reference', 'Return Reason', 'Settled At',
]


def unapplied_summary(payment_cycle_id=None, date_from=None, date_to=None, destination=None):
    from django.db.models import Count, DateTimeField, Min, Q, Sum
    from django.db.models.functions import Coalesce, TruncDate
    from tasaf_payment.returned import RESOLUTIONS, resolution_q

    qs = PaylistItem.objects.filter(is_deleted=False, status=PaylistItemStatus.UNAPPLIED)
    if payment_cycle_id:
        qs = qs.filter(paylist__payment_cycle_id=payment_cycle_id)
    if destination:
        qs = qs.filter(paylist__destination=destination)
    if date_from or date_to:
        returned = PaylistItem.objects.filter(id__in=qs.values('id')).annotate(returned_on=TruncDate(Coalesce(
            Min('return_feedbacks__received_at', filter=Q(return_feedbacks__is_deleted=False)),
            'date_updated', output_field=DateTimeField())))
        if date_from:
            returned = returned.filter(returned_on__gte=date_from)
        if date_to:
            returned = returned.filter(returned_on__lte=date_to)
        qs = PaylistItem.objects.filter(id__in=returned.values('id'))

    def totals(subset):
        row = subset.aggregate(count=Count('id'), amount=Sum('amount'))
        return {'count': row['count'] or 0, 'amount': float(row['amount'] or 0)}

    return {'total': totals(qs),
            'by_resolution': {r: totals(qs.filter(resolution_q(r))) for r in RESOLUTIONS}}


UNAPPLIED_EXPORT_COLUMNS = [
    'HHID', 'Payee', 'Account Number', 'FSP', 'Amount', 'MUSE Batch Reference', 'Reason', 'Received At',
    'Suggested Action', 'Action', 'Decided By', 'Decided At', 'Note', 'Progress', 'Region', 'District',
]
RESOLUTION_LABELS = {
    'AWAITING_ACTION': 'Awaiting action', 'IN_CORRECTION': 'In correction', 'REPAYING': 'Re-paying',
    'REPAID': 'Re-paid', 'UNAPPLIED_WITHDRAWN': 'Unapplied withdrawn', 'LAPSED': 'Lapsed',
}
ACTION_LABELS = {'REPAY': 'Repay', 'CASE': 'Send to Case', 'WITHDRAW': 'Withdraw'}


def export_unapplied_items(user, items):
    import uuid as _uuid

    from django.core.files.base import ContentFile
    from core.models import ExportableQueryModel
    from tasaf_payment.models import UNAPPLIED_ACTION_KEY
    from tasaf_payment.returned import resolution_of, suggested_action

    items = list(items.select_related(
        'paylist', 'payment_account__group_beneficiary__group__location__parent__parent__parent',
    ).prefetch_related('return_feedbacks').order_by('-date_created', 'id'))
    records = []
    for item in items:
        account = item.payment_account
        group = account.group_beneficiary.group if account and account.group_beneficiary_id else None
        feedbacks = sorted((f for f in item.return_feedbacks.all() if not f.is_deleted),
                           key=lambda f: f.received_at, reverse=True)
        feedback = feedbacks[0] if feedbacks else None
        decision = (item.json_ext or {}).get(UNAPPLIED_ACTION_KEY) or {}
        region, district, _, _ = _location_names(group.location if group else None)
        received = (item.json_ext or {}).get('returned_at') or (
            feedback.received_at.strftime('%Y-%m-%d %H:%M') if feedback else '')
        records.append(dict(zip(UNAPPLIED_EXPORT_COLUMNS, [
            group.code if group else '',
            account.account_name or '' if account else '',
            account.account_number if account else '',
            account.fsp_name if account else '',
            float(item.amount) if item.amount is not None else None,
            item.paylist.muse_batch_reference or '' if item.paylist_id else '',
            ' · '.join(p for p in [feedback.reason_code if feedback else None,
                                   feedback.reason_description if feedback else item.return_reason] if p),
            str(received)[:16].replace('T', ' '),
            ACTION_LABELS.get(suggested_action(item), ''),
            ACTION_LABELS.get(decision.get('action'), ''),
            decision.get('by', ''),
            str(decision.get('at', ''))[:16].replace('T', ' '),
            'Provider changed' if decision.get('auto') else decision.get('note') or '',
            RESOLUTION_LABELS.get(resolution_of(item), ''),
            region, district,
        ])))
    frame = pd.DataFrame.from_records(records, columns=UNAPPLIED_EXPORT_COLUMNS)

    filename = f"{_uuid.uuid4()}.csv"
    export = ExportableQueryModel(
        name=filename,
        model='PaylistItem',
        content=ContentFile(frame.to_csv(index=False), filename),
        user=user,
        sql_query='tasaf_payment.reports.export_unapplied_items',
        file_format=ExportableQueryModel.FileFormat.CSV,
    )
    export.save()
    logger.info("export_unapplied_items: %s (%d row(s))", filename, len(records))
    return export.name


def _location_names(location):
    chain = []
    while location is not None and len(chain) < 4:
        chain.insert(0, location.name)
        location = location.parent
    return (chain + [''] * 4)[:4]


def export_paylist_items(user, items):
    import uuid as _uuid

    from django.core.files.base import ContentFile
    from pandas import DataFrame
    from core.models import ExportableQueryModel
    from individual.models import GroupIndividual
    from tasaf_payment.muse.message import payee_code

    items = list(items.select_related(
        'benefit_consumption', 'payment_account__group_beneficiary__group__location__parent__parent__parent',
    ).order_by('date_created', 'id'))
    group_ids = {i.payment_account.group_beneficiary.group_id for i in items
                 if i.payment_account.group_beneficiary_id}
    names = {}
    for group_id, first, last in (GroupIndividual.objects
                                  .filter(group_id__in=group_ids, is_deleted=False, recipient_type='PRIMARY')
                                  .values_list('group_id', 'individual__first_name', 'individual__last_name')):
        names.setdefault(group_id, f"{first or ''} {last or ''}".strip())

    def amount(value):
        return float(value) if value is not None else None

    records = []
    for item in items:
        account = item.payment_account
        group = account.group_beneficiary.group if account.group_beneficiary_id else None
        records.append(dict(zip(PAYLIST_ITEM_EXPORT_COLUMNS, [
            group.code if group else '',
            payee_code(group),
            names.get(group.id, '') if group else '',
            item.benefit_consumption.code if item.benefit_consumption_id else '',
            account.account_number, account.account_name or '', account.fsp_name, account.fsp_type,
            *_location_names(group.location if group else None),
            amount(item.amount), amount(item.net_amount), amount(item.charge_amount),
            item.status, item.muse_reference or '', item.return_reason or '',
            item.settled_at.strftime('%Y-%m-%d %H:%M') if item.settled_at else '',
        ])))
    frame = DataFrame.from_records(records, columns=PAYLIST_ITEM_EXPORT_COLUMNS)

    filename = f"{_uuid.uuid4()}.csv"
    export = ExportableQueryModel(
        name=filename,
        model='PaylistItem',
        content=ContentFile(frame.to_csv(index=False), filename),
        user=user,
        sql_query='tasaf_payment.reports.export_paylist_items',
        file_format=ExportableQueryModel.FileFormat.CSV,
    )
    export.save()
    logger.info("export_paylist_items: %s (%d row(s))", filename, len(records))
    return export.name


def paylist_pdf_rows(paylist):
    from tasaf_payment.muse.message import _representative_names

    names = _representative_names(paylist)
    rows = (paylist.items.filter(is_deleted=False).exclude(status=PaylistItemStatus.WITHDRAWN)
            .order_by('date_created', 'id')
            .values_list('payment_account__group_beneficiary__group_id',
                         'payment_account__group_beneficiary__group__code',
                         'payment_account__group_beneficiary__group__json_ext__primary_recipient',
                         'payment_account__fsp_name', 'payment_account__account_number', 'amount'))
    return [{
        'hhid': code or '',
        'representative': names.get(group_id) or str(primary or '').strip(),
        'fsp': fsp or '',
        'account': account or '',
        'amount': float(amount) if amount is not None else 0,
    } for group_id, code, primary, fsp, account, amount in rows]


PDF_DEDUCTION_KEYS = ('education_penalty', 'health_penalty', 'overpayment')


def paylist_pdf_breakdown(paylist):
    payments, deductions = defaultdict(Decimal), defaultdict(Decimal)
    gross = charges = Decimal('0')
    rows = (paylist.items.filter(is_deleted=False).exclude(status=PaylistItemStatus.WITHDRAWN)
            .values_list('amount', 'charge_amount', 'benefit_consumption__json_ext'))
    for amount, charge, ext in rows:
        gross += _dec(amount)
        charges += _dec(charge)
        breakdown = (ext or {}).get('pct_breakdown') or {}
        for key, value in breakdown.items():
            if key in PDF_DEDUCTION_KEYS or key.endswith('_penalty'):
                deductions[key] += _dec(value)
            elif key.endswith('_amount') and key != 'household_cap_amount':
                payments[key] += _dec(value)
        if breakdown:
            deductions['household_cap'] += _dec(breakdown.get('raw_total')) - _dec(breakdown.get('capped_total'))
    payments['withdrawal_charge'] += charges
    difference = gross - (sum(payments.values()) - sum(deductions.values()))
    if difference > 0:
        payments['other'] += difference
    elif difference < 0:
        deductions['other'] -= difference

    def listed(side):
        return [{'key': key, 'amount': float(value)} for key, value in side.items() if value]

    return {'payments': listed(payments), 'deductions': listed(deductions), 'total': float(gross)}
