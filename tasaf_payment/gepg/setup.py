import re

from tasaf_payment.muse.setup import SetupError, propose, provider_list, server_environment

TOPIC = 'tasaf.verification.gepg'
SETTINGS_FIELDS = ('sp_code', 'sp_sys_id')
FIELD_LABELS = {'sp_code': 'SP code', 'sp_sys_id': 'SP system ID'}
CHANGE_EFFECT = ('Applies to every account verification sent to GePG from this server once approved. '
                 'Requests already sent are not affected.')
SP_CODE_RE = re.compile(r'^[A-Za-z0-9]{1,5}$')
SP_SYS_ID_RE = re.compile(r'^[A-Za-z0-9]{1,10}$')


def get_settings():
    from tasaf_payment.models import GepgSettings
    return GepgSettings.objects.filter(is_deleted=False).order_by('-date_created').first()


def settings_values():
    s = get_settings()
    return {f: getattr(s, f) for f in SETTINGS_FIELDS} if s else {}


def normalise_settings(data):
    values = {'sp_code': (data.get('sp_code') or '').strip().upper(),
              'sp_sys_id': (data.get('sp_sys_id') or '').strip().upper()}
    errors = []
    if values['sp_code'] and not SP_CODE_RE.match(values['sp_code']):
        errors.append('SP code must be up to 5 letters or digits')
    if values['sp_sys_id'] and not SP_SYS_ID_RE.match(values['sp_sys_id']):
        errors.append('SP system ID must be up to 10 letters or digits')
    if errors:
        raise SetupError('; '.join(errors))
    return values


def propose_settings(user, data, reason=''):
    from tasaf_payment.models import MuseChangeKind
    return propose(user, MuseChangeKind.GEPG_SETTINGS, normalise_settings(data), reason=reason)


def apply(change, user):
    from tasaf_payment.models import GepgSettings
    target = get_settings() or GepgSettings()
    for field in SETTINGS_FIELDS:
        if field in change.proposed:
            setattr(target, field, change.proposed[field])
    target.environment = server_environment()
    target.save(username=user.username)


def api_code():
    from django.conf import settings
    return ((getattr(settings, 'ESB', None) or {}).get('API_CODES') or {}).get(TOPIC, '')


def govesb_on():
    try:
        from coremis_app_integration.govesb import govesb_enabled
    except ImportError:
        return False
    return govesb_enabled()


def readiness():
    s = get_settings()
    settings_missing = [f for f in SETTINGS_FIELDS if not (getattr(s, f) if s else '')]
    providers_missing = sorted(p['fsp_code'] for p in provider_list() if p['accounts'] and not p['bic'])
    server = server_environment()
    approved_on = s.environment if s else ''
    environment_mismatch = bool(s) and approved_on != server
    enabled, code = govesb_on(), api_code()
    return {'settings_missing': settings_missing, 'providers_missing': providers_missing,
            'server_environment': server, 'settings_environment': approved_on,
            'environment_mismatch': environment_mismatch, 'govesb_enabled': enabled,
            'api_code_set': bool(code), 'topic': TOPIC,
            'ready': enabled and bool(code) and not settings_missing and not environment_mismatch}
