import re

from tasaf_payment import msisdn

PREFIX_RE = re.compile(r'^0[67]\d{1,2}$')
INVALID_MOBILE = 'INVALID_MOBILE'
PROVIDER_MISMATCH = 'PROVIDER_MISMATCH'


def normalise_prefix(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if digits.startswith('255'):
        digits = digits[3:]
    if digits and not digits.startswith('0'):
        digits = '0' + digits
    return digits


def normalise_list(values):
    if isinstance(values, str):
        values = re.split(r'[\s,;]+', values)
    out = []
    for value in values or []:
        prefix = normalise_prefix(value)
        if prefix and prefix not in out:
            out.append(prefix)
    return out


def invalid(prefixes):
    return [p for p in prefixes if not PREFIX_RE.match(p)]


def table():
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.models import FspProfile

    out = {p.fsp_code: list(p.mobile_prefixes) for p in FspProfile.objects.filter(is_deleted=False)
           if p.mobile_prefixes}
    for code, prefixes in (TasafPaymentConfig.mobile_prefixes or {}).items():
        out.setdefault(code, normalise_list(prefixes))
    return out


def clashes(fsp_code, prefixes, current=None):
    current = table() if current is None else current
    taken = {}
    for code, owned in current.items():
        if code == fsp_code:
            continue
        for prefix in owned:
            taken.setdefault(prefix, code)
    return {p: taken[p] for p in prefixes if p in taken}


def owner(number, current=None):
    national = '0' + number[3:] if msisdn.is_valid(number) else ''
    if not national:
        return None, None
    current = table() if current is None else current
    best = (None, None)
    for code, owned in current.items():
        for prefix in owned:
            if national.startswith(prefix) and len(prefix) > len(best[1] or ''):
                best = (code, prefix)
    return best


def check_enabled():
    from tasaf_payment.apps import TasafPaymentConfig
    return bool(getattr(TasafPaymentConfig, 'check_mobile_prefix', True))


def problem(account, current=None):
    from tasaf_payment.charges import resolve_fsp_code

    if account.fsp_type != 'MOBILE':
        return None
    number = msisdn.normalise(account.account_number)
    if not msisdn.is_valid(number):
        return {'code': INVALID_MOBILE, 'number': account.account_number or '',
                'reason': f"'{account.account_number or ''}' is not a mobile number (255 followed by 9 digits)"}
    if not check_enabled():
        return None
    saved = resolve_fsp_code(account.fsp_name) or ''
    belongs_to, prefix = owner(number, current)
    if not belongs_to or belongs_to == saved:
        return None
    return {'code': PROVIDER_MISMATCH, 'number': number, 'prefix': prefix, 'belongs_to': belongs_to,
            'saved': saved,
            'reason': f'Number {number} is {belongs_to} ({prefix}) but the account is saved as {saved or account.fsp_name}'}
