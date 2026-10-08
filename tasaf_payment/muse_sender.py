"""Sends a paylist's MUSE BULK_PAYMENT message over GovESB: checked, ids frozen, retried, logged.
The paylist becomes SUBMITTED only when GovESB accepts the message."""
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from tasaf_payment import muse_message

logger = logging.getLogger(__name__)

SEND = 'SEND'
RECORD_ONLY = 'RECORD_ONLY'

SENT = 'SENT'
REFUSED = 'REFUSED'
FAILED = 'FAILED'
UNKNOWN = 'UNKNOWN'
RECORDED = 'RECORDED'

RETRYABLE_HTTP = (429, 500, 502, 503, 504)


class SendResult:
    def __init__(self, outcome, message='', problems=None, attempts=0, request_id=None, reply=None):
        self.outcome = outcome
        self.message = message
        self.problems = problems or []
        self.attempts = attempts
        self.request_id = request_id
        self.reply = reply

    @property
    def submitted(self):
        return self.outcome in (SENT, RECORDED)


def _config(name, default):
    from tasaf_payment.apps import TasafPaymentConfig
    value = getattr(TasafPaymentConfig, name, None)
    return default if value in (None, '') else value


def _log(paylist, body, attempt, status, **fields):
    try:
        from muse_payment_adaptor.models import MuseTransactionLog
    except ImportError:
        return None
    header = body['message']['messageHeader']
    summary = body['message']['paymentSummary']
    try:
        return MuseTransactionLog.objects.create(
            transaction_type='BULK_PAYMENT', direction='OUT', status=status,
            attempt_number=attempt, msg_id=header['msgId'],
            batch_reference=summary['referenceNo'], paylist_uuid=str(paylist.uuid),
            item_count=summary['noofTransaction'], amount=Decimal(str(summary['totalAmount'])), **fields)
    except Exception as exc:  # noqa: BLE001 — the audit row must never stop a send
        logger.warning("Could not write MuseTransactionLog: %s", exc)
        return None


def _close_log(entry, status, **fields):
    if not entry:
        return
    try:
        entry.status = status
        for key, value in fields.items():
            setattr(entry, key, value)
        entry.save(update_fields=['status', *fields.keys()])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update MuseTransactionLog: %s", exc)


def _freeze_ids(paylist, body, user):
    """Store msgId, referenceNo and the message time on the first attempt; later attempts and
    re-submits then rebuild the identical header."""
    header = body['message']['messageHeader']
    changed = []
    if not paylist.muse_msg_id:
        paylist.muse_msg_id = header['msgId']
        changed.append('muse_msg_id')
    if not paylist.muse_batch_reference:
        paylist.muse_batch_reference = body['message']['paymentSummary']['referenceNo']
        changed.append('muse_batch_reference')
    ext = dict(paylist.json_ext or {})
    if 'muse_created_at' not in ext:
        ext['muse_created_at'] = header['createdAt']
        paylist.json_ext = ext
        changed.append('json_ext')
    if changed:
        paylist.save(user=user)


def _log_payees(body):
    """HHID next to payeeCode for every payee, for audit and reconciliation."""
    msg_id = body['message']['messageHeader']['msgId']
    for payee in body['message'].get('payList') or []:
        logger.info("MUSE payee msgId=%s endtoEndId=%s hhid=%s payeeCode=%s", msg_id,
                    payee.get('endtoEndId'), muse_message.hhid_of(payee.get('payeeCode')),
                    payee.get('payeeCode'))


def muse_reply(esb_body):
    """MUSE's own message (its ACK) when it came back as the reply to our send, else None."""
    message = esb_body.get('message') if isinstance(esb_body, dict) else None
    if isinstance(message, dict) and isinstance(message.get('messageHeader'), dict):
        return esb_body
    return None


def apply_reply(result):
    """Run MUSE's reply ACK through the inbound handler, once the paylist is SUBMITTED.
    Returns the ACK status (RECEIVED / REJECTED) or None; never raises."""
    if not result or not result.reply:
        return None
    try:
        from tasaf_payment.muse_inbound import handle
        handle(result.reply)
        return str(((result.reply['message'].get('messageSummary') or {}).get('status')) or '') or None
    except Exception as exc:  # noqa: BLE001 — the batch is sent; a reply problem must not undo that
        logger.warning("Could not apply MUSE's reply ACK: %s", exc)
        return None


def request_type():
    from coremis_app_integration.esb_client import ESBRequestType
    name = str(_config('muse_request_type', 'NORMAL')).upper()
    return ESBRequestType.PUSH if name == 'PUSH' else ESBRequestType.NORMAL


def _remember(paylist, user, result):
    ext = dict(paylist.json_ext or {})
    ext['muse_last_send'] = {
        'outcome': result.outcome, 'message': result.message[:500], 'attempts': result.attempts,
        'request_id': result.request_id, 'at': datetime.now(tz=timezone.utc).isoformat(),
    }
    paylist.json_ext = ext
    paylist.save(user=user)


class MuseSender:
    def __init__(self, user, sleep=time.sleep):
        self.user = user
        self.sleep = sleep

    def send(self, paylist, topic):
        mode = str(_config('muse_submit_mode', SEND)).upper()
        body = muse_message.build(paylist)

        if mode == RECORD_ONLY:
            _freeze_ids(paylist, body, self.user)
            body = muse_message.build(paylist)
            problems = muse_message.check(body)
            _log(paylist, body, 1, 'NOT_SENT',
                 error_message=f'muse_submit_mode=RECORD_ONLY; {len(problems)} schema problem(s)')
            result = SendResult(RECORDED, 'Recorded as submitted without sending (RECORD_ONLY)')
            _remember(paylist, self.user, result)
            return result

        problems = muse_message.check(body)
        if problems:
            summary = muse_message.summarise(problems)
            text = '; '.join(f"{p['section']}.{p['field']}: {p['message']} ({p['count']})" for p in summary[:6])
            return SendResult(REFUSED, f'MUSE would reject this message — {text}', problems=summary)

        try:
            from coremis_app_integration.govesb import GovESBProducer, govesb_enabled
            from coremis_app_integration.esb_client.exceptions import (
                ESBConfigurationError, ESBSignatureError,
            )
        except ImportError:
            return SendResult(REFUSED, 'The GovESB adaptor is not installed; nothing was sent')
        if not govesb_enabled():
            return SendResult(REFUSED, 'GovESB is not configured on this server; nothing was sent')

        _freeze_ids(paylist, body, self.user)
        body = muse_message.build(paylist)
        _log_payees(body)
        max_attempts = max(1, int(_config('muse_send_max_attempts', 3)))
        backoff = float(_config('muse_send_backoff_seconds', 2))
        result = None

        for attempt in range(1, max_attempts + 1):
            entry = _log(paylist, body, attempt, 'PENDING')
            try:
                # No user_id: it would wrap esbBody as {"Payload": ...} and break MUSE's schema.
                response = GovESBProducer().publish(topic, body, request_type=request_type())
            except ESBConfigurationError as exc:
                _close_log(entry, 'FAILED', error_message=str(exc)[:2000])
                result = SendResult(FAILED, f'GovESB is misconfigured: {exc}', attempts=attempt)
                break
            except ESBSignatureError as exc:
                _close_log(entry, 'UNKNOWN', error_message=str(exc)[:2000])
                result = SendResult(UNKNOWN, 'GovESB answered but its signature could not be verified; '
                                             'the message may have arrived — submit again to resend it '
                                             'safely (same message id)', attempts=attempt)
                break
            except Exception as exc:  # noqa: BLE001 — transport / token errors are retried
                _close_log(entry, 'RETRYING' if attempt < max_attempts else 'FAILED',
                           error_message=str(exc)[:2000])
                result = SendResult(FAILED, f'Could not reach GovESB: {exc}', attempts=attempt)
                if attempt < max_attempts:
                    self.sleep(backoff ** attempt)
                continue

            status_code = response.get('status_code')
            response_text = json.dumps(response.get('esb_body'), default=str)[:4000]
            if not response.get('published'):
                _close_log(entry, 'FAILED', error_message='GovESB disabled')
                result = SendResult(REFUSED, 'GovESB is not configured on this server; nothing was sent',
                                    attempts=attempt)
                break
            if response.get('ok'):
                _close_log(entry, 'SUCCESS', http_status_code=status_code, response_body=response_text,
                           esb_request_id=str(response.get('request_id') or ''))
                result = SendResult(SENT, 'Sent to MUSE', attempts=attempt,
                                    request_id=response.get('request_id'),
                                    reply=muse_reply(response.get('esb_body')))
                break
            error = str(response.get('error') or f'HTTP {status_code}')
            if status_code in RETRYABLE_HTTP and attempt < max_attempts:
                _close_log(entry, 'RETRYING', http_status_code=status_code, response_body=response_text,
                           error_message=error[:2000])
                result = SendResult(FAILED, f'GovESB error: {error}', attempts=attempt)
                self.sleep(backoff ** attempt)
                continue
            _close_log(entry, 'REJECTED' if status_code and status_code < 500 else 'FAILED',
                       http_status_code=status_code, response_body=response_text,
                       error_message=error[:2000])
            result = SendResult(FAILED, f'GovESB refused the message: {error}', attempts=attempt)
            break

        _remember(paylist, self.user, result)
        logger.info("MUSE send paylist=%s outcome=%s attempts=%s msgId=%s",
                    paylist.uuid, result.outcome, result.attempts, paylist.muse_msg_id)
        return result
