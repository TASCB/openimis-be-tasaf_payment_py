"""MUSE BULK_PAYMENT message: built from an approved paylist, checked against MUSE's schema
before anything is sent."""
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

from tasaf_payment.muse_setup import BIC_RE, CURRENCY
from tasaf_payment.payee_code import PayeeCodeError, decode_payee_code, encode_hhid

SENDER = 'TASAFMIS'
RECEIVER = 'MUSE'
MESSAGE_TYPE = 'BULK_PAYMENT'
PAYMENT_TYPE = 'PAYMENT'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
CHANNELS = ('BANK', 'MOBILE')

MIN_AMOUNT = Decimal('0.01')
MAX_AMOUNT = Decimal('999999999999999.99')

HEADER_REQUIRED = ('sender', 'receiver', 'msgId', 'messageType', 'paymentType', 'createdAt')
SUMMARY_REQUIRED = ('institutionCode', 'referenceNo', 'paymentDesc', 'currencyCode', 'applyDate',
                    'totalAmount', 'noofTransaction', 'isSTP', 'subBudgetClass', 'payerAccount')
PAYEE_REQUIRED = ('payeeCode', 'payeeName', 'payeeAccountNumber', 'payeeAccountName',
                  'payeeBankName', 'payeeBankBic', 'amount', 'endtoEndId', 'paymentChannel')

NAME_RE = re.compile(r"^[0-9a-zA-Z.,/\-' ]{1,150}$")
PATTERNS = {
    'currencyCode': (re.compile(r'^[A-Z]{3}$'), 'three capital letters'),
    'payeeCode': (re.compile(r'^[0-9a-zA-Z]{1,15}$'), 'letters and digits only, at most 15'),
    'payeeName': (NAME_RE, 'at most 150 letters, digits, spaces and . , / - \''),
    'payeeAccountName': (NAME_RE, 'at most 150 letters, digits, spaces and . , / - \''),
    'payeeAccountNumber': (re.compile(r'^[0-9a-zA-Z]{6,35}$'), '6 to 35 letters and digits'),
    'payeeBankBic': (BIC_RE, 'Tanzanian BIC, e.g. NMIBTZTZ'),
    'endtoEndId': (re.compile(r'^[0-9A-Za-z]{1,16}$'), 'letters and digits only, at most 16'),
}


# ── Identifiers ─────────────────────────────────────────────────────────────

def attempt(paylist):
    return int((paylist.json_ext or {}).get('muse_attempt') or 1)


def msg_id(paylist):
    return (getattr(paylist, 'muse_msg_id', None)
            or f"TM{paylist.uuid.hex[:12].upper()}{attempt(paylist):02d}")


def reference_no(paylist):
    if getattr(paylist, 'muse_batch_reference', None):
        return paylist.muse_batch_reference
    created = paylist.date_created or datetime.now()
    return f"TP{created:%y%m%d}-{paylist.uuid.hex[:8].upper()}"


def message_time(paylist):
    """The time a sent message was first built; resends reuse it."""
    frozen = (paylist.json_ext or {}).get('muse_created_at')
    return datetime.strptime(frozen, DATE_FORMAT) if frozen else None


def payee_code(group):
    """The household's HHID encoded for MUSE; empty when it is not a P3 HHID, which ``check``
    reports. The raw HHID is never sent."""
    try:
        return encode_hhid(getattr(group, 'code', None))
    except PayeeCodeError:
        return ''


def financial_year(when):
    return str(when.year + 1 if when.month >= 7 else when.year)


def money(value):
    """A 2-decimal JSON number; whole amounts without a fraction."""
    q = Decimal(value or 0).quantize(Decimal('0.01'))
    return int(q) if q == q.to_integral_value() else float(q)


# ── Building ────────────────────────────────────────────────────────────────

def settings_values(settings):
    if settings is None:
        return {'institutionCode': '', 'payerAccount': '', 'subBudgetClass': None,
                'paymentDesc': '', 'currencyCode': CURRENCY, 'isSTP': 'False', 'gl_accounts': []}
    return {
        'institutionCode': settings.institution_code,
        'payerAccount': settings.payer_account,
        'subBudgetClass': settings.sub_budget_class,
        'paymentDesc': settings.payment_desc,
        'currencyCode': CURRENCY,
        'isSTP': 'True' if settings.is_stp else 'False',
        'gl_accounts': settings.gl_accounts or [],
    }


def assemble(ids, settings, payees, now):
    """The message itself, from plain values. ``ids`` holds msgId and referenceNo."""
    total = sum((Decimal(str(p['amount'])) for p in payees), Decimal('0'))
    stamp = now.strftime(DATE_FORMAT)
    summary = {
        'institutionCode': settings['institutionCode'],
        # The spec's schema spells it institutioncode, its example institutionCode; send both.
        'institutioncode': settings['institutionCode'],
        'referenceNo': ids['referenceNo'],
        'paymentDesc': settings['paymentDesc'],
        'currencyCode': settings['currencyCode'],
        'applyDate': stamp,
        'totalAmount': money(total),
        'noofTransaction': len(payees),
        'isSTP': settings['isSTP'],
        'subBudgetClass': settings['subBudgetClass'],
        'payerAccount': settings['payerAccount'],
    }
    message = {
        'messageHeader': {
            'sender': SENDER, 'receiver': RECEIVER, 'msgId': ids['msgId'],
            'messageType': MESSAGE_TYPE, 'paymentType': PAYMENT_TYPE, 'createdAt': stamp,
        },
        'paymentSummary': summary,
        'payList': payees,
    }
    gl = settings['gl_accounts']
    if gl:
        message['glList'] = [{
            'glaccount': line.get('glaccount', ''),
            'glaccountDesc': line.get('glaccountDesc', ''),
            'financialYear': financial_year(now),
            'reference': ids['referenceNo'],
            # One line takes the whole batch; a split across several has no agreed rule.
            'amount': money(total) if len(gl) == 1 else None,
            'grantName': line.get('grantName', ''),
        } for line in gl]
    # Required by the spec's schema; empty until MUSE says what to sign and with which key.
    return {'message': message, 'digitalSignature': ''}


def _representative_names(paylist):
    from individual.models import GroupIndividual

    group_ids = paylist.items.filter(is_deleted=False).values(
        'payment_account__group_beneficiary__group_id')
    names = {}
    rows = (GroupIndividual.objects
            .filter(group_id__in=group_ids, is_deleted=False, recipient_type='PRIMARY')
            .values_list('group_id', 'individual__first_name', 'individual__last_name'))
    for group_id, first, last in rows:
        name = f"{first or ''} {last or ''}".strip()
        if name:
            names.setdefault(group_id, name)
    return names


def _fsp_profiles(accounts):
    from tasaf_payment.charges import ChargeError, resolve_fsp_code
    from tasaf_payment.models import FspProfile

    profiles = {p.fsp_code: p for p in FspProfile.objects.filter(is_deleted=False)}
    by_name = {}
    for account in accounts:
        if account.fsp_name in by_name:
            continue
        try:
            code = resolve_fsp_code(account.fsp_name)
        except ChargeError:
            code = None
        by_name[account.fsp_name] = profiles.get(code)
    return by_name


def payee_rows(paylist):
    items = list(paylist.items.filter(is_deleted=False)
                 .select_related('payment_account__group_beneficiary__group', 'benefit_consumption')
                 .order_by('date_created', 'id'))
    names = _representative_names(paylist)
    profiles = _fsp_profiles([i.payment_account for i in items])
    rows = []
    for item in items:
        account = item.payment_account
        group = getattr(account.group_beneficiary, 'group', None)
        name = names.get(getattr(group, 'id', None)) or str(
            ((getattr(group, 'json_ext', None) or {}).get('primary_recipient')) or '').strip()
        profile = profiles.get(account.fsp_name)
        benefit = item.benefit_consumption
        rows.append({
            'payeeCode': payee_code(group),
            'payeeName': name,
            'payeeAccountNumber': account.account_number or '',
            'payeeAccountName': (account.account_name or '').strip() or name,
            'payeeBankName': profile.bank_name if profile else '',
            'payeeBankBic': profile.bic if profile else '',
            'amount': money(item.amount),
            'endtoEndId': benefit.code if benefit else '',
            'paymentChannel': ((profile.fsp_type if profile else '') or account.fsp_type or '').upper(),
        })
    return rows


def build(paylist, now=None):
    from tasaf_payment.muse_setup import get_settings

    now = now or message_time(paylist) or datetime.now()
    ids = {'msgId': msg_id(paylist), 'referenceNo': reference_no(paylist)}
    return assemble(ids, settings_values(get_settings()), payee_rows(paylist), now)


# ── Checking ────────────────────────────────────────────────────────────────

def _problem(section, field, rule, message, value=None, index=None, payee=None):
    out = {'section': section, 'field': field, 'rule': rule, 'message': message, 'value': value}
    if index is not None:
        out['index'] = index
        out['endtoEndId'] = (payee or {}).get('endtoEndId')
        out['payeeCode'] = (payee or {}).get('payeeCode')
        out['hhid'] = hhid_of(out['payeeCode'])
    return out


def hhid_of(code):
    try:
        return decode_payee_code(code)
    except PayeeCodeError:
        return None


def _missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _amount_problem(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return 'not a number'
    if d != d.quantize(Decimal('0.01')):
        return 'more than 2 decimals'
    if not MIN_AMOUNT <= d <= MAX_AMOUNT:
        return 'outside 0.01 – 999,999,999,999,999.99'
    return None


def check(body):
    """Everything in ``body`` that MUSE's schema would reject, plus the batch totals and the
    GL lines. An empty list means the message is sendable as far as we can tell."""
    problems = []
    message = (body or {}).get('message') or {}
    header = message.get('messageHeader') or {}
    summary = message.get('paymentSummary') or {}
    payees = message.get('payList') or []

    for field in HEADER_REQUIRED:
        if _missing(header.get(field)):
            problems.append(_problem('messageHeader', field, 'required', 'Required'))

    for field in SUMMARY_REQUIRED:
        if _missing(summary.get(field)):
            problems.append(_problem('paymentSummary', field, 'required',
                                     'Required — enter it in MUSE settings'
                                     if field in ('institutionCode', 'payerAccount',
                                                  'subBudgetClass', 'paymentDesc') else 'Required'))
    currency = summary.get('currencyCode')
    if not _missing(currency) and not PATTERNS['currencyCode'][0].match(str(currency)):
        problems.append(_problem('paymentSummary', 'currencyCode', 'pattern',
                                 PATTERNS['currencyCode'][1], currency))
    if not isinstance(summary.get('isSTP'), str):
        problems.append(_problem('paymentSummary', 'isSTP', 'type', 'Must be a string',
                                 summary.get('isSTP')))

    if not payees:
        problems.append(_problem('payList', 'payList', 'required', 'The paylist has no payments'))
    if summary.get('noofTransaction') != len(payees):
        problems.append(_problem('paymentSummary', 'noofTransaction', 'mismatch',
                                 f'Does not match the {len(payees)} payments in payList',
                                 summary.get('noofTransaction')))
    try:
        total = sum((Decimal(str(p.get('amount'))) for p in payees), Decimal('0'))
        if Decimal(str(summary.get('totalAmount'))) != total:
            problems.append(_problem('paymentSummary', 'totalAmount', 'mismatch',
                                     f'Does not match the sum of payList ({money(total)})',
                                     summary.get('totalAmount')))
    except (InvalidOperation, TypeError, ValueError):
        pass

    seen = Counter(p.get('endtoEndId') for p in payees if not _missing(p.get('endtoEndId')))
    for index, payee in enumerate(payees):
        for field in PAYEE_REQUIRED:
            value = payee.get(field)
            if _missing(value):
                problems.append(_problem('payList', field, 'required',
                                         'No P3 HHID (P3-#########-########) to encode'
                                         if field == 'payeeCode' else 'Required',
                                         index=index, payee=payee))
                continue
            if field in PATTERNS and not PATTERNS[field][0].match(str(value)):
                problems.append(_problem('payList', field, 'pattern', PATTERNS[field][1],
                                         value, index, payee))
        if not _missing(payee.get('amount')):
            reason = _amount_problem(payee['amount'])
            if reason:
                problems.append(_problem('payList', 'amount', 'range', reason,
                                         payee['amount'], index, payee))
        channel = payee.get('paymentChannel')
        if not _missing(channel) and channel not in CHANNELS:
            problems.append(_problem('payList', 'paymentChannel', 'choice', 'BANK or MOBILE',
                                     channel, index, payee))
        if seen.get(payee.get('endtoEndId'), 0) > 1:
            problems.append(_problem('payList', 'endtoEndId', 'duplicate',
                                     'Used by more than one payment in this batch',
                                     payee.get('endtoEndId'), index, payee))

    for index, line in enumerate(message.get('glList') or []):
        if _missing(line.get('glaccount')):
            problems.append(_problem('glList', 'glaccount', 'required', 'Required', index=index))
        if line.get('amount') is None:
            problems.append(_problem('glList', 'amount', 'undecided',
                                     'More than one GL line and no agreed rule for splitting '
                                     'the batch total between them', index=index))
    return problems


def summarise(problems):
    """One row per (section, field, rule) with a count, so 50 000 identical failures read as
    one line."""
    counts = Counter((p['section'], p['field'], p['rule'], p['message']) for p in problems)
    return [{'section': s, 'field': f, 'rule': r, 'message': m, 'count': n}
            for (s, f, r, m), n in counts.most_common()]


# ── Preview ─────────────────────────────────────────────────────────────────

def _request_type_name():
    try:
        from tasaf_payment.muse_sender import request_type
        return request_type().name
    except ImportError:
        return 'NORMAL'


def preview(paylist, sample_rows=20, max_problems=100):
    """The message a MUSE submit of this paylist would carry and what MUSE would reject in
    it. Read-only; payList and problems are trimmed for display, counts are not."""
    from django.conf import settings as django_settings
    from tasaf_payment.services import PaylistService

    topic = PaylistService.GOVESB_TOPIC_PAYMENT_SUBMIT_BY_DESTINATION.get(
        paylist.destination, PaylistService.GOVESB_TOPIC_PAYMENT_SUBMIT)
    try:
        from coremis_app_integration.govesb import GovESBProducer, govesb_enabled
        enabled = govesb_enabled()
        api_code = GovESBProducer._resolve_api_code(topic)
    except ImportError:
        enabled, api_code = False, None

    from tasaf_payment.muse_setup import get_settings, server_environment

    body = build(paylist)
    problems = check(body)
    stored = get_settings()
    server = server_environment()
    if stored is not None and stored.environment != server:
        problems.insert(0, _problem('settings', 'environment', 'environment',
                                    'MUSE settings were approved on another server',
                                    f"{stored.environment or '—'} ≠ {server or '—'}"))
    message = body['message']
    shown = {**message, 'payList': message['payList'][:sample_rows]}
    return {
        'paylist_uuid': str(paylist.uuid),
        'status': paylist.status,
        'destination': paylist.destination,
        'topic': topic,
        'api_code': api_code,
        'api_code_mapped': bool(api_code) and api_code != topic,
        'govesb_enabled': enabled,
        'esb_url': ((getattr(django_settings, 'ESB', None) or {}).get('ENGINE_URL') if enabled else None),
        'request_type': _request_type_name(),
        'msg_id': message['messageHeader']['msgId'],
        'reference_no': message['paymentSummary']['referenceNo'],
        'attempt': attempt(paylist),
        'payments': len(message['payList']),
        'rows_shown': len(shown['payList']),
        'valid': not problems,
        'problem_count': len(problems),
        'problem_summary': summarise(problems),
        'problems': problems[:max_problems],
        'body': {'requestdata': {'message': shown, 'digitalSignature': body.get('digitalSignature', '')}},
    }
