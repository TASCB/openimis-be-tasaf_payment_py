import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from tasaf_payment.models import UNAPPLIED_ACTION_KEY, PaylistItem, PaylistItemStatus, UnappliedAction
from tasaf_payment.muse import message as muse_message

logger = logging.getLogger(__name__)

MESSAGE_TYPE = 'UNAPPLIED_WITHDRAW'
TOPIC = 'tasaf.payment.unapplied.withdrawal'
KEY = 'muse_withdrawal'
MSG_PREFIX = 'TW'
SENT, RECEIVED, REJECTED, RECORDED, SENDING = 'SENT', 'RECEIVED', 'REJECTED', 'RECORDED', 'SENDING'
SEND_LOCK_MINUTES = 10  # frees payees whose request died mid-send
REASON_MAX, NAME_MAX = 255, 100
PROVIDER_CHANGED_REASON = 'Provider changed'
STOP_AFTER_FAILURES = 3  # GovESB down: stop instead of timing out on each


def pending():
    withdrawn = PaylistItem.objects.filter(
        is_deleted=False, status=PaylistItemStatus.UNAPPLIED,
        **{f'json_ext__{UNAPPLIED_ACTION_KEY}__action': UnappliedAction.WITHDRAW})
    stale = (datetime.now(tz=timezone.utc) - timedelta(minutes=SEND_LOCK_MINUTES)).isoformat()
    return withdrawn.filter(
        ~Q(json_ext__has_key=KEY)
        | Q(**{f'json_ext__{KEY}__status': REJECTED})
        | Q(**{f'json_ext__{KEY}__status': SENDING, f'json_ext__{KEY}__sending_at__lt': stale}))


def msg_id():
    return f"{MSG_PREFIX}{uuid.uuid4().hex[:14].upper()}"


def payment_reference(item):
    """The endtoEndId MUSE returned (a re-send's own id)."""
    return muse_message.end_to_end_id(item)


def reason(item):
    decision = (item.json_ext or {}).get(UNAPPLIED_ACTION_KEY) or {}
    text = PROVIDER_CHANGED_REASON if decision.get('auto') else (decision.get('note') or 'Withdrawn by TASAF')
    return text[:REASON_MAX]


def _full_name(login):
    from core.models import User
    user = User.objects.filter(username=login).select_related('i_user').first() if login else None
    i_user = getattr(user, 'i_user', None)
    name = ' '.join(p for p in (getattr(i_user, 'other_names', None), getattr(i_user, 'last_name', None)) if p)
    return name.strip() or login or ''


def withdrawn_by(item):
    decision = (item.json_ext or {}).get(UNAPPLIED_ACTION_KEY) or {}
    login = decision.get('changed_by') if decision.get('auto') else None
    return _full_name(login or decision.get('by'))[:NAME_MAX]


def build(item, message_id, now=None):
    stamp = (now or datetime.now()).strftime(muse_message.DATE_FORMAT)
    return {'message': {
        'messageHeader': {'sender': muse_message.SENDER, 'receiver': muse_message.RECEIVER, 'msgId': message_id,
                          'messageType': MESSAGE_TYPE, 'createdAt': stamp},
        'messageDetails': {'paymentReference': payment_reference(item), 'withdrawReason': reason(item),
                           'withdrawnBy': withdrawn_by(item)},
    }, 'digitalSignature': ''}


def _log(item, body, status, **fields):
    try:
        from muse_payment_adaptor.models import MuseTransactionLog
    except ImportError:
        return
    try:
        MuseTransactionLog.objects.create(
            transaction_type=MESSAGE_TYPE, direction='OUT', status=status, attempt_number=1,
            msg_id=body['message']['messageHeader']['msgId'], item_count=1,
            amount=Decimal(str(item.amount or 0)), request_body=json.dumps(body, default=str), **fields)
    except Exception as exc:  # noqa: BLE001 — the audit row must never stop a send
        logger.warning("Could not write MuseTransactionLog: %s", exc)


def _stamp(items, user, **state):
    for item in items:
        item.json_ext = {**(item.json_ext or {}), KEY: {**((item.json_ext or {}).get(KEY) or {}), **state}}
        item.save(user=user)


class WithdrawalError(Exception):
    pass


def _producer():
    try:
        from coremis_app_integration.govesb import GovESBProducer, govesb_enabled
    except ImportError:
        raise WithdrawalError('The GovESB adaptor is not installed; nothing was sent')
    if not govesb_enabled():
        raise WithdrawalError('GovESB is not configured on this server; nothing was sent')
    return GovESBProducer()


def _claim(user):
    """Mark the pending payees SENDING under a row lock, so a second click finds none."""
    with transaction.atomic():
        items = list(pending().select_for_update(of=('self',)).select_related(
            'paylist', 'payment_account__group_beneficiary__group', 'benefit_consumption'))
        if not items:
            sending = PaylistItem.objects.filter(**{f'json_ext__{KEY}__status': SENDING}).first()
            if sending:
                by = (sending.json_ext or {}).get(KEY, {}).get('by')
                raise WithdrawalError(f'Withdrawals are already being sent to MUSE (by {by}); '
                                      f'wait for the result')
            raise WithdrawalError('There is no withdrawn unapplied payment to report to MUSE')
        previous = {item.pk: (item.json_ext or {}).get(KEY) for item in items}
        _stamp(items, user, status=SENDING, sending_at=datetime.now(tz=timezone.utc).isoformat(),
               by=getattr(user, 'username', None))
    return items, previous


def _release(items, user, previous):
    with transaction.atomic():
        for item in items:
            ext = dict(item.json_ext or {})
            if previous.get(item.pk) is None:
                ext.pop(KEY, None)
            else:
                ext[KEY] = previous[item.pk]
            item.json_ext = ext
            item.save(user=user)


def _send_one(item, user, producer, previous):
    from tasaf_payment.muse.sender import muse_reply, request_type

    message_id = msg_id()
    body = build(item, message_id)
    now = datetime.now(tz=timezone.utc).isoformat()
    if producer is None:
        _log(item, body, 'NOT_SENT', error_message='muse_submit_mode=RECORD_ONLY')
        _stamp([item], user, msg_id=message_id, sent_at=now, status=RECORDED)
        return None
    try:
        response = producer.publish(TOPIC, body, request_type=request_type())
    except Exception as exc:  # noqa: BLE001
        _log(item, body, 'FAILED', error_message=str(exc)[:2000])
        _release([item], user, previous)
        return str(exc)
    if not response.get('ok'):
        error = str(response.get('error') or f"HTTP {response.get('status_code')}")
        _log(item, body, 'FAILED', http_status_code=response.get('status_code'), error_message=error[:2000])
        _release([item], user, previous)
        return error
    _log(item, body, 'SUCCESS', http_status_code=response.get('status_code'),
         esb_request_id=str(response.get('request_id') or ''),
         response_body=json.dumps(response.get('esb_body'), default=str)[:4000])
    _stamp([item], user, msg_id=message_id, sent_at=now, status=SENT)
    reply = muse_reply(response.get('esb_body'))
    if reply:
        from tasaf_payment.muse.inbound import handle
        handle(reply)
    return None


def send(user):
    from tasaf_payment.muse.sender import RECORD_ONLY, SEND, muse_config

    record_only = str(muse_config('muse_submit_mode', SEND)).upper() == RECORD_ONLY
    producer = None if record_only else _producer()
    items, previous = _claim(user)
    result = {'sent': 0, 'failed': 0, 'skipped': 0, 'errors': []}
    in_a_row = 0
    for index, item in enumerate(items):
        if in_a_row >= STOP_AFTER_FAILURES:
            rest = items[index:]
            _release(rest, user, previous)
            result['failed'] += len(rest)
            break
        if not payment_reference(item):
            _release([item], user, previous)
            result['skipped'] += 1
            continue
        try:
            error = _send_one(item, user, producer, previous)
        except Exception as exc:  # noqa: BLE001
            _release([item], user, previous)
            error = str(exc)
        if error:
            result['failed'] += 1
            in_a_row += 1
            if error not in result['errors']:
                result['errors'].append(error)
        else:
            result['sent'] += 1
            in_a_row = 0
    logger.info("MUSE unapplied withdraw: %s", result)
    return result


def acknowledge(org_msg_id, status, status_desc=None):
    items = list(PaylistItem.objects.filter(**{f'json_ext__{KEY}__msg_id': org_msg_id}))
    if not items:
        return None
    from tasaf_payment.services import inbound_audit_user
    state = REJECTED if str(status or '').upper() == 'REJECTED' else RECEIVED
    with transaction.atomic():
        _stamp(items, inbound_audit_user(items[0]), status=state, status_desc=status_desc,
               answered_at=datetime.now(tz=timezone.utc).isoformat())
    return len(items)
