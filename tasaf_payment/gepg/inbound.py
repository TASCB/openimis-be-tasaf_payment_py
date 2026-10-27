import logging
from datetime import datetime, timezone

from django.db import transaction

from tasaf_payment.gepg import message as gepg_message

logger = logging.getLogger(__name__)


def _retry_codes():
    from tasaf_payment.apps import TasafPaymentConfig
    return {str(c) for c in (TasafPaymentConfig.gepg_retry_codes or [])}


def _apply_item(item, entry, acc_verif_id):
    from tasaf_payment.models import GepgItemStatus, VerificationStatus
    from tasaf_payment.services import MuseVerificationInboundService

    code = str(entry.get('AccStsCode') or '')
    desc = str(entry.get('AccStsDesc') or '')
    name = str(entry.get('AccName') or '').strip()
    item.acc_name, item.acc_sts_code, item.acc_sts_desc = name[:150], code[:10], desc[:255]
    item.answered_at = datetime.now(tz=timezone.utc)
    item.status = GepgItemStatus.ANSWERED

    account = item.payment_account
    if account.verification_status not in (VerificationStatus.PENDING_MUSE, VerificationStatus.PENDING):
        item.decision = 'IGNORED'
        return
    if code in _retry_codes():
        # Nobody compared the name: the account returns to its earlier status so it is sent again
        from tasaf_payment.services import _inbound_audit_user
        if account.verification_status == VerificationStatus.PENDING_MUSE:
            account.verification_status = item.previous_status
            account.save(user=_inbound_audit_user())
        item.decision = 'UNCHECKED'
        return
    payload = {'account_uuid': str(account.uuid), 'muse_reference': acc_verif_id,
               'returned_name': name, 'raw_response': {'channel': 'GEPG', **entry}}
    if code != gepg_message.SUCCESS or not name:
        payload['result'] = 'FAILED'
        payload['failure_reason'] = f"GePG {code or '(no code)'}: {desc or 'account could not be verified'}"
    result = MuseVerificationInboundService().handle_result(payload)
    if not result.get('success'):
        raise RuntimeError(result.get('error') or result.get('detail') or 'result not applied')
    account.refresh_from_db()
    item.decision = VerificationStatus(account.verification_status).name[:10]


def handle(payload):
    from tasaf_payment.models import GepgItemStatus, GepgRequestStatus, GepgVerificationRequest

    resp = gepg_message.find(payload, gepg_message.RESPONSE)
    if not resp:
        logger.warning("GePG inbound: not a %s message", gepg_message.RESPONSE)
        return False, f'Not a GePG {gepg_message.RESPONSE} message'
    acc_verif_id = str(resp.get('AccVerifId') or '').strip()
    try:
        with transaction.atomic():
            request = GepgVerificationRequest.objects.select_for_update().filter(
                acc_verif_id=acc_verif_id).first()
            if not request:
                logger.warning("GePG inbound: unknown AccVerifId %r", acc_verif_id)
                return False, f'Unknown AccVerifId {acc_verif_id!r}'
            items = {i.item_id: i for i in request.items.select_related('payment_account')}
            applied = duplicates = unmatched = 0
            for entry in gepg_message.items_of(resp):
                item = items.get(str(entry.get('AccVerifItemId') or ''))
                if not item or str(entry.get('AccNum') or '') != item.acc_num:
                    unmatched += 1
                    logger.warning("GePG inbound %s: no item %r with AccNum %r", acc_verif_id,
                                   entry.get('AccVerifItemId'), entry.get('AccNum'))
                    continue
                if item.status == GepgItemStatus.ANSWERED:
                    duplicates += 1
                    continue
                _apply_item(item, entry, acc_verif_id)
                item.save()
                applied += 1
            if applied or request.status != GepgRequestStatus.ANSWERED:
                request.status = GepgRequestStatus.ANSWERED
                request.answered_at = datetime.now(tz=timezone.utc)
                request.response_body = payload
                request.save(update_fields=['status', 'answered_at', 'response_body'])
            if applied:
                from collections import Counter
                from tasaf_payment.events import emit_gepg_answered
                emit_gepg_answered(request, dict(Counter(i.decision or 'PENDING' for i in items.values())))
    except Exception as exc:  # noqa: BLE001 — not acknowledged, so GePG sends it again
        logger.exception("GePG inbound %s failed", acc_verif_id)
        return False, f'Could not process the response: {exc}'
    logger.info("GePG inbound %s: %d applied, %d duplicate, %d unmatched", acc_verif_id, applied,
                duplicates, unmatched)
    return True, gepg_message.response_ack(acc_verif_id)
