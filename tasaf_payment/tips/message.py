import re
import uuid
from datetime import datetime

SINGLE, BULK = 'SINGLE', 'BULK'
TOPICS = {SINGLE: 'tasaf.verification.tips.single', BULK: 'tasaf.verification.tips.bulk'}
MESSAGE_TYPE = 'TIPSBULKLOOKUP'
MSG_ID_PREFIX = 'TASAF_'

BULK_TYPES = ('MSISDN', 'BANK', 'WALLET', 'ALIAS')
SINGLE_TYPES = BULK_TYPES + ('BUSINESS', 'CONTROLNO')
FSP_RE = re.compile(r'^\d{3}$')
MSISDN_RE = re.compile(r'^(0\d{9}|255\d{9})$')
IDENTIFIER_RE = re.compile(r'^[0-9A-Za-z]{1,50}$')

FOUND, NOT_FOUND, INVALID, EMPTY = 'FOUND', 'NOT_FOUND', 'INVALID', 'EMPTY'
INVALID_CODES = ('3100',)
ANSWER_TYPE = 'TIPSLOOKUP'


def new_msg_id(now=None):
    return f"{MSG_ID_PREFIX}{(now or datetime.now()).strftime('%d%m%Y%H%M%S')}{uuid.uuid4().hex[:4].upper()}"


def _header(settings, msg_id):
    return {'sender': settings.get('sender') or '', 'receiver': settings.get('receiver') or '',
            'messageType': MESSAGE_TYPE, 'msgId': msg_id}


def _lookup(identifier_type, identifier, fsp_code):
    return {'payloadType': 'JSON', 'identifierType': identifier_type, 'identifier': str(identifier),
            'fsp-destination': fsp_code}


def build_single(settings, msg_id, identifier_type, identifier, fsp_code):
    return {'message': {'messageHeader': _header(settings, msg_id),
                        'messageSummary': _lookup(identifier_type, identifier, fsp_code)},
            'digitalSignature': ''}


def build_bulk(settings, msg_id, items):
    summary = _lookup(settings.get('summary_identifier_type') or 'BANK', settings.get('summary_identifier') or '',
                      settings.get('summary_fsp_code') or '')
    details = [{'fspDestination': fsp, 'identifierType': kind, 'identifier': str(number)}
               for kind, number, fsp in items]
    return {'message': {'messageHeader': _header(settings, msg_id), 'messageSummary': summary,
                        'messageDetails': details, 'digitalSignature': ''}}


def _problem(field, message, value=None, index=None):
    return {'field': field, 'message': message, 'value': value, 'index': index}


def _check_lookup(kind_allowed, identifier_type, identifier, fsp_code, index=None, prefix=''):
    problems = []
    if identifier_type not in kind_allowed:
        allowed = ', '.join(kind_allowed)
        problems.append(_problem(f'{prefix}identifierType', f'One of {allowed}', identifier_type, index))
    if not FSP_RE.match(str(fsp_code or '')):
        problems.append(_problem(f'{prefix}fspDestination', 'Three-digit FSP code', fsp_code, index))
    pattern = MSISDN_RE if identifier_type == 'MSISDN' else IDENTIFIER_RE
    if not pattern.match(str(identifier or '')):
        rule = '0XXXXXXXXX or 255XXXXXXXXX' if identifier_type == 'MSISDN' else 'letters and digits, up to 50'
        problems.append(_problem(f'{prefix}identifier', f'Expected {rule}', identifier, index))
    return problems


def check(body, kind):
    message = (body or {}).get('message') or {}
    header = message.get('messageHeader') or {}
    problems = [_problem(f, 'Required') for f in ('sender', 'receiver', 'messageType', 'msgId') if not header.get(f)]
    if header.get('msgId') and not str(header['msgId']).startswith(MSG_ID_PREFIX):
        problems.append(_problem('msgId', f'Must start with {MSG_ID_PREFIX}', header['msgId']))
    summary = message.get('messageSummary') or {}
    if kind == SINGLE:
        return problems + _check_lookup(SINGLE_TYPES, summary.get('identifierType'), summary.get('identifier'),
                                        summary.get('fsp-destination'))
    details = message.get('messageDetails') or []
    if not details:
        problems.append(_problem('messageDetails', 'No accounts'))
    seen = set()
    for i, item in enumerate(details):
        problems += _check_lookup(BULK_TYPES, item.get('identifierType'), item.get('identifier'),
                                  item.get('fspDestination'), i, 'messageDetails.')
        key = (item.get('fspDestination'), item.get('identifier'))
        if key in seen:
            problems.append(_problem('messageDetails.identifier', 'Listed twice', item.get('identifier'), i))
        seen.add(key)
    return problems


def read_result(entry):
    answer = entry.get('response')
    if isinstance(answer, dict):
        entry = {**entry, **answer}
    error = entry.get('errorInformation') or {}
    code = str(error.get('errorCode') or '')
    if entry.get('fullName'):
        outcome = FOUND
    elif code:
        outcome = INVALID if code in INVALID_CODES else NOT_FOUND
    else:
        outcome = EMPTY
    return {'outcome': outcome, 'identifier_type': entry.get('identifierType') or '',
            'identifier': str(entry.get('identifier') or ''),
            'fsp_code': entry.get('fspId') or entry.get('fspDestination') or '',
            'full_name': entry.get('fullName') or '', 'lookup_id': entry.get('lookupId') or '',
            'error_code': code, 'error_description': error.get('errorDescription') or ''}


def normalise_identifier(identifier_type, identifier):
    value = re.sub(r'\s+', '', str(identifier or ''))
    if identifier_type == 'MSISDN' and re.fullmatch(r'0\d{9}', value):
        return '255' + value[1:]
    return value


def ack(header, accepted=True, reason=''):
    return {'message': {
        'messageHeader': {'sender': 'TASAF MIS', 'receiver': (header or {}).get('sender') or '',
                          'msgId': new_msg_id(), 'messageType': 'ACK'},
        'messageSummary': {'orgMsgId': (header or {}).get('msgId') or '',
                           'status': 'RECEIVED' if accepted else 'REJECTED',
                           'statusDesc': 'Received Successfully' if accepted else reason[:255]}}}


def without_identity(body):
    if isinstance(body, dict):
        return {k: without_identity(v) for k, v in body.items() if k != 'identity'}
    if isinstance(body, list):
        return [without_identity(v) for v in body]
    return body


def results_in(body):
    message = (body or {}).get('message') if isinstance(body, dict) else None
    if not isinstance(message, dict):
        return []
    details = message.get('messageDetails')
    if isinstance(details, list) and details:
        return [read_result(e) for e in details if isinstance(e, dict)]
    summary = message.get('messageSummary') or {}
    answered = (message.get('messageHeader') or {}).get('messageType') == ANSWER_TYPE and summary.get('identifier')
    if summary.get('fullName') or summary.get('errorInformation') or answered:
        return [read_result(summary)]
    return []
