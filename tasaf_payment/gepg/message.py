import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape

REQUEST = 'gepgAccVerifSubReq'
REQUEST_ACK = 'gepgAccVerifSubReqAck'
RESPONSE = 'gepgAccVerifSubResp'
RESPONSE_ACK = 'gepgAccVerifSubRespAck'
SUCCESS = '7101'
XML, JSON = 'XML', 'JSON'

REQUEST_ORDER = ('SpCode', 'SpSysId', 'AccVerifId', 'Bic', 'AccVerifItems')
ITEM_ORDER = ('AccVerifItemId', 'AccNum')
LIMITS = {'SpCode': 5, 'SpSysId': 10, 'AccVerifId': 50, 'Bic': 10, 'AccVerifItemId': 50, 'AccNum': 50}
MOBILE_RE = re.compile(r'^255\d{9}$')
BANK_RE = re.compile(r'^[0-9A-Za-z]{1,50}$')
BIC_RE = re.compile(r'^[A-Z0-9]{8,10}$')


def new_acc_verif_id(now=None):
    stamp = (now or datetime.now()).strftime('%Y%m%d%H%M%S')
    return f"TV{stamp}{uuid.uuid4().hex[:8].upper()}"


def build(request, settings, items):
    return {REQUEST: {
        'SpCode': settings.get('sp_code') or '',
        'SpSysId': settings.get('sp_sys_id') or '',
        'AccVerifId': request.acc_verif_id,
        'Bic': request.bic,
        'AccVerifItems': [{'AccVerifItemId': i.item_id, 'AccNum': i.acc_num} for i in items],
    }}


def _problem(field, message, value=None, index=None):
    return {'field': field, 'message': message, 'value': value, 'index': index}


def check(body, fsp_type=''):
    req = (body or {}).get(REQUEST) or {}
    problems = []
    if tuple(req) != REQUEST_ORDER:
        problems.append(_problem('message', 'Tags missing or out of order', list(req)))
    for field in ('SpCode', 'SpSysId', 'AccVerifId', 'Bic'):
        value = str(req.get(field) or '')
        if not value:
            problems.append(_problem(field, 'Required'))
        elif len(value) > LIMITS[field]:
            problems.append(_problem(field, f'Longer than {LIMITS[field]} characters', value))
    if req.get('Bic') and not BIC_RE.match(str(req['Bic'])):
        problems.append(_problem('Bic', 'Not a BIC', req['Bic']))
    items = req.get('AccVerifItems') or []
    if not items:
        problems.append(_problem('AccVerifItems', 'No accounts'))
    seen = set()
    for i, item in enumerate(items):
        if tuple(item) != ITEM_ORDER:
            problems.append(_problem('AccVerifItem', 'Tags missing or out of order', list(item), i))
        item_id, acc_num = str(item.get('AccVerifItemId') or ''), str(item.get('AccNum') or '')
        if not item_id or len(item_id) > LIMITS['AccVerifItemId']:
            problems.append(_problem('AccVerifItemId', 'Required, up to 50 characters', item_id, i))
        elif item_id in seen:
            problems.append(_problem('AccVerifItemId', 'Not unique in the request', item_id, i))
        seen.add(item_id)
        pattern = MOBILE_RE if fsp_type == 'MOBILE' else BANK_RE
        if not pattern.match(acc_num):
            rule = '12-digit mobile number 255XXXXXXXXX' if fsp_type == 'MOBILE' else 'letters and digits only'
            problems.append(_problem('AccNum', f'Expected {rule}', acc_num, i))
    return problems


def summarise(problems):
    counts = {}
    for p in problems:
        key = (p['field'], p['message'])
        counts[key] = counts.get(key, 0) + 1
    return [{'field': f, 'message': m, 'count': n} for (f, m), n in counts.items()]


def _xml(tag, value):
    if isinstance(value, list):
        inner = ''.join(_xml('AccVerifItem', v) for v in value)
        return f'<{tag}>{inner}</{tag}>'
    if isinstance(value, dict):
        return f'<{tag}>' + ''.join(_xml(k, v) for k, v in value.items()) + f'</{tag}>'
    return f'<{tag}>{escape(str(value or ""), {chr(34): "&quot;", chr(39): "&apos;"})}</{tag}>'


def as_xml(body):
    (tag, content), = body.items()
    return f'<Gepg>{_xml(tag, content)}</Gepg>'


def message_format():
    from tasaf_payment.apps import TasafPaymentConfig
    return JSON if str(getattr(TasafPaymentConfig, 'gepg_message_format', '') or '').upper() == JSON else XML


def serialize(body, fmt=None):
    return as_xml(body) if (fmt or message_format()) == XML else body


def _from_xml(node):
    children = list(node)
    if not children:
        return (node.text or '').strip()
    if node.tag == 'AccVerifItems':
        return [_from_xml(c) for c in children]
    return {c.tag: _from_xml(c) for c in children}


def parse_xml(text):
    root = ET.fromstring(text)
    return {root.tag: _from_xml(root)}


def load(body):
    if isinstance(body, bytes):
        body = body.decode('utf-8', 'replace')
    if isinstance(body, str):
        text = body.strip()
        if text[:1] in ('{', '['):
            try:
                return json.loads(text)
            except ValueError:
                return None
        try:
            return parse_xml(text)
        except ET.ParseError:
            return None
    return body


def find(payload, tag):
    payload = load(payload)
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get(tag), dict):
        return payload[tag]
    gepg = payload.get('Gepg')
    if isinstance(gepg, dict) and isinstance(gepg.get(tag), dict):
        return gepg[tag]
    return None


def items_of(message):
    raw = (message or {}).get('AccVerifItems')
    if isinstance(raw, dict):
        raw = raw.get('AccVerifItem', raw)
    if isinstance(raw, dict):
        raw = [raw]
    return [i for i in raw or [] if isinstance(i, dict)]


def response_ack(acc_verif_id, code=SUCCESS, desc='Successful'):
    return {RESPONSE_ACK: {'AccVerifId': acc_verif_id, 'AccVerifStsCode': code, 'AccVerifStsDesc': desc}}
