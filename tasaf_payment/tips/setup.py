import re

from tasaf_payment.muse.setup import SetupError, propose, provider_list, server_environment
from tasaf_payment.tips.message import BULK, BULK_TYPES, IDENTIFIER_RE, SINGLE, TOPICS

ROUTES = ('GEPG', 'TIPS')
SETTINGS_FIELDS = ('sender', 'receiver', 'summary_identifier_type', 'summary_identifier', 'summary_fsp_code')
OPTIONAL_FIELDS = ('fsp_codes', 'verification_route')
ALL_FIELDS = SETTINGS_FIELDS + OPTIONAL_FIELDS
FIELD_LABELS = {'sender': 'Sender code', 'receiver': 'Receiver code',
                'summary_identifier_type': 'Bulk summary identifier type',
                'summary_identifier': 'Bulk summary identifier', 'summary_fsp_code': 'Bulk summary FSP code',
                'fsp_codes': 'TIPS FSP codes', 'verification_route': 'Account verification goes to'}
CHANGE_EFFECT = ('Applies to every TIPS lookup sent from this server once approved, and to where account '
                 'verification goes. Lookups already sent are not affected.')
PARTY_RE = re.compile(r'^[A-Za-z0-9]{1,20}$')
FSP_RE = re.compile(r'^\d{3}$')


def get_settings():
    from tasaf_payment.models import TipsSettings
    return TipsSettings.objects.filter(is_deleted=False).order_by('-date_created').first()


def settings_values():
    s = get_settings()
    return {f: getattr(s, f) for f in ALL_FIELDS} if s else {}


def route():
    return settings_values().get('verification_route') or 'GEPG'


def normalise_settings(data):
    from tasaf_payment.charges import normalise_fsp
    values = {f: (data.get(f) or '').strip() for f in SETTINGS_FIELDS}
    for f in ('sender', 'receiver', 'summary_identifier_type'):
        values[f] = values[f].upper()
    values['verification_route'] = (data.get('verification_route') or 'GEPG').strip().upper()
    codes = data.get('fsp_codes') or {}
    values['fsp_codes'] = {normalise_fsp(k): str(v).strip() for k, v in codes.items() if str(v or '').strip()}
    errors = []
    for f in ('sender', 'receiver'):
        if values[f] and not PARTY_RE.match(values[f]):
            errors.append(f'{FIELD_LABELS[f]} must be up to 20 letters or digits')
    if values['summary_identifier_type'] and values['summary_identifier_type'] not in BULK_TYPES:
        errors.append(f"Bulk summary identifier type must be one of {', '.join(BULK_TYPES)}")
    if values['summary_identifier'] and not IDENTIFIER_RE.match(values['summary_identifier']):
        errors.append('Bulk summary identifier must be up to 50 letters or digits')
    if values['summary_fsp_code'] and not FSP_RE.match(values['summary_fsp_code']):
        errors.append('Bulk summary FSP code must be three digits')
    bad = sorted(k for k, v in values['fsp_codes'].items() if not FSP_RE.match(v))
    if bad:
        errors.append(f"TIPS FSP codes must be three digits: {', '.join(bad)}")
    if values['verification_route'] not in ROUTES:
        errors.append('Account verification must go to GEPG or TIPS')
    missing = [FIELD_LABELS[f] for f in SETTINGS_FIELDS if not values[f]]
    if values['verification_route'] == 'TIPS' and missing:
        errors.append(f"Verification cannot go to TIPS until these are set: {', '.join(missing)}")
    if errors:
        raise SetupError('; '.join(errors))
    return values


def propose_settings(user, data, reason=''):
    from tasaf_payment.models import MuseChangeKind
    return propose(user, MuseChangeKind.TIPS_SETTINGS, normalise_settings(data), reason=reason)


def apply(change, user):
    from tasaf_payment.models import TipsSettings
    target = get_settings() or TipsSettings()
    for field in ALL_FIELDS:
        if field in change.proposed:
            setattr(target, field, change.proposed[field])
    target.environment = server_environment()
    target.save(username=user.username)


def api_code(kind):
    from django.conf import settings
    return ((getattr(settings, 'ESB', None) or {}).get('API_CODES') or {}).get(TOPICS[kind], '')


def govesb_on():
    from tasaf_payment.gepg.setup import govesb_on as on
    return on()


def readiness(providers=None):
    s = get_settings()
    values = settings_values()
    settings_missing = [f for f in SETTINGS_FIELDS if not values.get(f)]
    codes = values.get('fsp_codes') or {}
    providers_missing = sorted(p['fsp_code'] for p in (providers if providers is not None else provider_list())
                               if p['accounts'] and not codes.get(p['fsp_code']))
    server = server_environment()
    approved_on = s.environment if s else ''
    mismatch = bool(s) and approved_on != server
    enabled = govesb_on()
    single, bulk = bool(api_code(SINGLE)), bool(api_code(BULK))
    return {'settings_missing': settings_missing, 'providers_missing': providers_missing,
            'server_environment': server, 'settings_environment': approved_on, 'environment_mismatch': mismatch,
            'govesb_enabled': enabled, 'api_code_single_set': single, 'api_code_bulk_set': bulk,
            'topics': dict(TOPICS), 'route': route(),
            'ready': enabled and bulk and not settings_missing and not mismatch}
