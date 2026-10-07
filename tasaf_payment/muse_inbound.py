"""MUSE's messages to TASAF MIS — ACK / RESPONSE (batch) and PAYMENT_STATUS (per payment):
matched to our paylist or item, applied, logged, and answered with TASAF's ACK
(REJECTED with the reason when refused)."""
import json
import logging
import re
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

BATCH_STATUSES = {'RECEIVED': 'RECEIVED', 'REJECTED': 'REJECTED', 'ACCEPTED': 'ACCEPTED',
                  'SENT TO BANK': 'SENT_TO_BANK'}
PAYMENT_STATUSES = ('SETTLED', 'UNAPPLIED')
REFERENCE_RE = re.compile(r'Reference\s+([A-Za-z0-9-]+)', re.IGNORECASE)
ACK_TOPIC = 'tasaf.payment.ack'


class Reply:
    def __init__(self, http, body, log_status='SUCCESS', note=''):
        self.http, self.body, self.log_status, self.note = http, body, log_status, note


def _error(http, text, log_status):
    return Reply(http, {'success': False, 'error': text}, log_status, text)


def bank_reference(text):
    match = REFERENCE_RE.search(text or '')
    return match.group(1) if match else None


def ack_message(org_message_type, org_msg_id, desc='Received Successfully', status='RECEIVED'):
    from tasaf_payment.muse_message import DATE_FORMAT, RECEIVER, SENDER
    return {'message': {
        'messageHeader': {'sender': SENDER, 'receiver': RECEIVER,
                          'msgId': f"TA{uuid.uuid4().hex[:14].upper()}",
                          'messageType': 'ACK', 'createdAt': datetime.now().strftime(DATE_FORMAT)},
        'messageSummary': {'orgMessageType': org_message_type, 'orgMsgId': org_msg_id,
                           'status': status, 'statusDesc': desc},
    }}


def _log(message_type, status, raw, note='', paylist=None, benefit_code='', their_msg_id='',
         direction='IN'):
    try:
        from muse_payment_adaptor.models import MuseTransactionLog
        MuseTransactionLog.objects.create(
            transaction_type=message_type if message_type in ('ACK', 'RESPONSE', 'PAYMENT_STATUS') else 'ACK',
            direction=direction, status=status, benefit_code=benefit_code or '',
            muse_reference=their_msg_id or None,
            msg_id=getattr(paylist, 'muse_msg_id', None) or '',
            batch_reference=getattr(paylist, 'muse_batch_reference', None) or '',
            paylist_uuid=str(paylist.uuid) if paylist else '',
            response_body=json.dumps(raw, default=str)[:4000], error_message=note[:2000] or None)
    except Exception as exc:  # noqa: BLE001 — the audit row must never lose the message
        logger.warning("Could not write inbound MuseTransactionLog: %s", exc)


def _push_ack(ack, paylist):
    from tasaf_payment.apps import TasafPaymentConfig
    if not getattr(TasafPaymentConfig, 'muse_ack_push', False):
        return
    try:
        from coremis_app_integration.govesb import GovESBProducer
        result = GovESBProducer().publish(ACK_TOPIC, ack)
        _log('ACK', 'SUCCESS' if result.get('ok') else 'FAILED', ack, paylist=paylist, direction='OUT',
             note='' if result.get('ok') else str(result.get('error') or 'not sent'))
    except Exception as exc:  # noqa: BLE001 — the synchronous ACK already answered MUSE
        logger.warning("MUSE ACK push failed: %s", exc)
        _log('ACK', 'FAILED', ack, str(exc), paylist=paylist, direction='OUT')


def _acknowledge(message_type, header, paylist):
    if message_type == 'ACK':
        return {'success': True, 'received': header.get('msgId')}
    ack = ack_message(message_type, header.get('msgId'))
    _push_ack(ack, paylist)
    return ack


def _reject(message_type, header, paylist, reason):
    ack = ack_message(message_type, header.get('msgId'), reason, status='REJECTED')
    if message_type != 'ACK':
        _push_ack(ack, paylist)
    return ack


def _batch(message_type, header, summary):
    from tasaf_payment.models import Paylist, PaymentDestination
    from tasaf_payment.services import _parse_dt, apply_muse_batch_status

    status = BATCH_STATUSES.get(str(summary.get('status') or '').strip().upper().replace('_', ' '))
    if not status:
        return None, _error(400, f"Unknown batch status: {summary.get('status')!r}", 'FAILED')
    org_msg_id = str(summary.get('orgMsgId') or '').strip()
    paylist = Paylist.objects.filter(muse_msg_id=org_msg_id, is_deleted=False,
                                     destination=PaymentDestination.MUSE).first() if org_msg_id else None
    if not paylist:
        return None, _error(404, f"No paylist was sent with msgId {org_msg_id!r}", 'REJECTED')
    applied = apply_muse_batch_status(paylist, status, summary.get('statusDesc'),
                                      _parse_dt(header.get('createdAt')))
    note = '' if applied else f'{status} not applied: paylist is {paylist.status}'
    return paylist, Reply(200, _acknowledge(message_type, header, paylist), 'SUCCESS', note)


def _find_item(end_to_end_id, org_msg_id):
    """orgMsgId narrows the search only when it names one of our batches; MUSE's spec also
    sends null or the payment's own reference there."""
    from tasaf_payment.models import (
        PAYLIST_IN_FLIGHT_STATUSES, PaylistItem, PaylistItemStatus, PaymentDestination,
    )
    items = PaylistItem.objects.filter(
        benefit_consumption__code=end_to_end_id, is_deleted=False,
        paylist__is_deleted=False, paylist__destination=PaymentDestination.MUSE,
        paylist__muse_msg_id__isnull=False,
    ).exclude(paylist__muse_msg_id='').select_related('paylist', 'payment_account__group_beneficiary__group')
    if org_msg_id and items.filter(paylist__muse_msg_id=org_msg_id).exists():
        items = items.filter(paylist__muse_msg_id=org_msg_id)
    return (items.filter(paylist__status__in=PAYLIST_IN_FLIGHT_STATUSES, status=PaylistItemStatus.PENDING)
            .order_by('-date_created').first()
            or items.order_by('-date_created').first())


def _payee_problem(details, item):
    """MUSE's spec matches payments on endToEndId only. If a payeeCode comes along anyway, it
    must decode to an existing HHID, the one this payment was sent for; otherwise the message is
    refused and nothing is applied. The decoded HHID is what we log; payeeCode is never stored."""
    from individual.models import Group
    from tasaf_payment.payee_code import PayeeCodeError, decode_payee_code

    if 'payeeCode' not in details:
        return None
    code = details.get('payeeCode')
    try:
        hhid = decode_payee_code(code)
    except PayeeCodeError as exc:
        logger.error("MUSE inbound payeeCode refused: %s", exc)
        return _error(400, str(exc), 'FAILED')
    if not Group.objects.filter(code=hhid, is_deleted=False).exists():
        logger.error("MUSE inbound payeeCode %s decodes to unknown household %s", code, hhid)
        return _error(404, f"No household {hhid} for payeeCode {code}", 'REJECTED')
    group = getattr(getattr(item.payment_account, 'group_beneficiary', None), 'group', None)
    if getattr(group, 'code', None) != hhid:
        logger.error("MUSE inbound payeeCode %s (household %s) does not match payment %s",
                     code, hhid, item.benefit_consumption_id)
        return _error(409, f"payeeCode {code} is household {hhid}, not the household this "
                           f"payment was sent for", 'REJECTED')
    return None


def _payment(message_type, header, details):
    from tasaf_payment.models import PAYLIST_IN_FLIGHT_STATUSES, PaylistItemStatus
    from tasaf_payment.services import settle_item, unapply_item

    status = str(details.get('status') or '').strip().upper()
    end_to_end_id = str(details.get('endToEndId') or '').strip()
    if status not in PAYMENT_STATUSES:
        return None, end_to_end_id, _error(400, f"Unknown payment status: {details.get('status')!r}", 'FAILED')
    item = _find_item(end_to_end_id, str(details.get('orgMsgId') or '').strip()) if end_to_end_id else None
    if not item:
        return None, end_to_end_id, _error(404, f"No MUSE payment with endToEndId {end_to_end_id!r}", 'REJECTED')

    paylist = item.paylist
    refused = _payee_problem(details, item)
    if refused:
        return paylist, end_to_end_id, refused
    wanted = PaylistItemStatus.PROCESSED if status == 'SETTLED' else PaylistItemStatus.UNAPPLIED
    if item.status == wanted:
        return paylist, end_to_end_id, Reply(200, _acknowledge(message_type, header, paylist), 'SUCCESS',
                                             'duplicate: already recorded')
    if item.status != PaylistItemStatus.PENDING:
        return paylist, end_to_end_id, _error(
            409, f"Payment {end_to_end_id} is already {item.status}; {status} not applied", 'REJECTED')
    if paylist.status not in PAYLIST_IN_FLIGHT_STATUSES:
        return paylist, end_to_end_id, _error(
            409, f"Paylist is {paylist.status}, not with MUSE; {status} not applied", 'REJECTED')

    desc = details.get('statusDesc')
    if status == 'SETTLED':
        settle_item(item, header.get('createdAt'), bank_reference(desc))
    else:
        unapply_item(item, None, desc, bank_reference(desc))
    return paylist, end_to_end_id, Reply(200, _acknowledge(message_type, header, paylist))


def handle(payload):
    """One MUSE message (the verified GovESB business payload) -> (success, esbBody or message).
    success=False only for non-MUSE input and our own failures."""
    message = payload.get('message') if isinstance(payload, dict) else None
    header = (message or {}).get('messageHeader') if isinstance(message, dict) else None
    if not isinstance(header, dict):
        _log('ACK', 'FAILED', payload, 'Not a MUSE message: no message.messageHeader')
        return False, 'Not a MUSE message: no message.messageHeader'

    message_type = str(header.get('messageType') or '').strip().upper()
    paylist, end_to_end_id = None, ''
    try:
        if isinstance(message.get('messageDetails'), dict):
            paylist, end_to_end_id, reply = _payment('PAYMENT_STATUS', header, message['messageDetails'])
            message_type = 'PAYMENT_STATUS'
        elif isinstance(message.get('messageSummary'), dict):
            paylist, reply = _batch(message_type if message_type in ('ACK', 'RESPONSE') else 'RESPONSE',
                                    header, message['messageSummary'])
        else:
            reply = _error(400, 'Message has neither messageSummary nor messageDetails', 'FAILED')
    except Exception as exc:  # noqa: BLE001 — log what arrived before failing the request
        logger.exception("MUSE inbound message failed")
        reply = _error(500, f'Could not process the message: {exc}', 'FAILED')

    _log(message_type, reply.log_status, payload, reply.note, paylist, end_to_end_id, header.get('msgId'))
    logger.info("MUSE inbound %s msgId=%s -> %s %s", message_type, header.get('msgId'),
                reply.http, reply.note)
    if reply.http == 200:
        return True, reply.body
    if reply.http == 500:
        return False, 'Could not process the message; please resend later'
    return True, _reject(message_type, header, paylist, reply.note)
