import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from tasaf_payment.models import UNAPPLIED_ACTION_KEY, PaylistItem, PaylistItemStatus, UnappliedAction
from tasaf_payment.muse import message as muse_message

logger = logging.getLogger(__name__)

MESSAGE_TYPE = 'UNAPPLIED_WITHDRAWAL'
TOPIC = 'tasaf.payment.unapplied.withdrawal'
KEY = 'muse_withdrawal'
MSG_PREFIX = 'TW'
SENT, RECEIVED, REJECTED, RECORDED = 'SENT', 'RECEIVED', 'REJECTED', 'RECORDED'


def pending():
    withdrawn = PaylistItem.objects.filter(
        is_deleted=False, status=PaylistItemStatus.UNAPPLIED,
        **{f'json_ext__{UNAPPLIED_ACTION_KEY}__action': UnappliedAction.WITHDRAW})
    return withdrawn.filter(~Q(json_ext__has_key=KEY) | Q(**{f'json_ext__{KEY}__status': REJECTED}))


def msg_id():
    return f"{MSG_PREFIX}{uuid.uuid4().hex[:14].upper()}"


def build(items, message_id, now=None):
    from tasaf_payment.muse.setup import get_settings
    settings = muse_message.settings_values(get_settings())
    stamp = (now or datetime.now()).strftime(muse_message.DATE_FORMAT)
    rows = []
    for item in items:
        group = getattr(getattr(item.payment_account, 'group_beneficiary', None), 'group', None)
        decision = (item.json_ext or {}).get(UNAPPLIED_ACTION_KEY) or {}
        rows.append({
            'orgEndToEndId': muse_message.end_to_end_id(item),
            'orgReferenceNo': item.paylist.muse_batch_reference or '',
            'payeeCode': muse_message.payee_code(group),
            'amount': muse_message.money(item.amount),
            'reason': 'Provider changed' if decision.get('auto') else (decision.get('note') or 'Withdrawn by TASAF'),
        })
    total = sum((Decimal(str(r['amount'])) for r in rows), Decimal('0'))
    return {'message': {
        'messageHeader': {'sender': muse_message.SENDER, 'receiver': muse_message.RECEIVER, 'msgId': message_id,
                          'messageType': MESSAGE_TYPE, 'createdAt': stamp},
        'withdrawalSummary': {'institutionCode': settings['institutionCode'], 'noofTransaction': len(rows),
                              'totalAmount': muse_message.money(total)},
        'withdrawalList': rows,
    }, 'digitalSignature': ''}


def _log(body, status, **fields):
    try:
        from muse_payment_adaptor.models import MuseTransactionLog
    except ImportError:
        return
    summary = body['message']['withdrawalSummary']
    try:
        MuseTransactionLog.objects.create(
            transaction_type=MESSAGE_TYPE, direction='OUT', status=status, attempt_number=1,
            msg_id=body['message']['messageHeader']['msgId'], item_count=summary['noofTransaction'],
            amount=Decimal(str(summary['totalAmount'])), request_body=json.dumps(body, default=str), **fields)
    except Exception as exc:  # noqa: BLE001 — the audit row must never stop a send
        logger.warning("Could not write MuseTransactionLog: %s", exc)


def _stamp(items, user, **state):
    for item in items:
        item.json_ext = {**(item.json_ext or {}), KEY: {**((item.json_ext or {}).get(KEY) or {}), **state}}
        item.save(user=user)


class WithdrawalError(Exception):
    pass


def send(user):
    from tasaf_payment.muse.sender import RECORD_ONLY, SEND, _config, muse_reply, request_type

    items = list(pending().select_related('paylist', 'payment_account__group_beneficiary__group',
                                          'benefit_consumption'))
    if not items:
        raise WithdrawalError('There is no withdrawn unapplied payment to report to MUSE')
    message_id = msg_id()
    body = build(items, message_id)
    now = datetime.now(tz=timezone.utc).isoformat()

    if str(_config('muse_submit_mode', SEND)).upper() == RECORD_ONLY:
        _log(body, 'NOT_SENT', error_message='muse_submit_mode=RECORD_ONLY')
        with transaction.atomic():
            _stamp(items, user, msg_id=message_id, sent_at=now, status=RECORDED)
        return message_id, len(items)

    try:
        from coremis_app_integration.govesb import GovESBProducer, govesb_enabled
    except ImportError:
        raise WithdrawalError('The GovESB adaptor is not installed; nothing was sent')
    if not govesb_enabled():
        raise WithdrawalError('GovESB is not configured on this server; nothing was sent')
    try:
        response = GovESBProducer().publish(TOPIC, body, request_type=request_type())
    except Exception as exc:  # noqa: BLE001
        _log(body, 'FAILED', error_message=str(exc)[:2000])
        raise WithdrawalError(f'Could not send the withdrawal list: {exc}')
    if not response.get('ok'):
        error = str(response.get('error') or f"HTTP {response.get('status_code')}")
        _log(body, 'FAILED', http_status_code=response.get('status_code'), error_message=error[:2000])
        raise WithdrawalError(f'GovESB refused the withdrawal list: {error}')

    _log(body, 'SUCCESS', http_status_code=response.get('status_code'),
         esb_request_id=str(response.get('request_id') or ''),
         response_body=json.dumps(response.get('esb_body'), default=str)[:4000])
    with transaction.atomic():
        _stamp(items, user, msg_id=message_id, sent_at=now, status=SENT)
    reply = muse_reply(response.get('esb_body'))
    if reply:
        from tasaf_payment.muse.inbound import handle
        handle(reply)
    logger.info("MUSE unapplied withdrawal list msgId=%s: %d payment(s)", message_id, len(items))
    return message_id, len(items)


def acknowledge(org_msg_id, status, status_desc=None):
    items = list(PaylistItem.objects.filter(**{f'json_ext__{KEY}__msg_id': org_msg_id}))
    if not items:
        return None
    from tasaf_payment.services import _inbound_audit_user
    state = REJECTED if str(status or '').upper() == 'REJECTED' else RECEIVED
    with transaction.atomic():
        _stamp(items, _inbound_audit_user(items[0]), status=state, status_desc=status_desc,
               answered_at=datetime.now(tz=timezone.utc).isoformat())
    return len(items)
