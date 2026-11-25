import logging
import re
from collections import Counter
from datetime import datetime, timezone

from django.db import transaction

from tasaf_payment.tips import message as tips_message
from tasaf_payment.verification_summary import answered_desc

logger = logging.getLogger(__name__)

WAITING = ('SENT', 'ACCEPTED')
MATCHABLE = WAITING + ('ANSWERED',)


def _key(kind, identifier, fsp):
    return tips_message.normalise_identifier(kind or 'BANK', identifier), str(fsp or '')


def _apply_item(item, result, ref):
    from tasaf_payment.models import GepgItemStatus, VerificationStatus
    from tasaf_payment.services import MuseVerificationInboundService, inbound_audit_user

    item.full_name = result['full_name'][:150]
    item.lookup_id = result['lookup_id'][:100]
    item.error_code, item.error_description = result['error_code'][:10], result['error_description'][:255]
    item.answered_at = datetime.now(tz=timezone.utc)
    item.status = GepgItemStatus.ANSWERED
    account = item.payment_account
    if account is None:
        from tasaf_payment.account_check import apply_tips_answer
        apply_tips_answer(item, result)
        return
    open_statuses = (VerificationStatus.PENDING_MUSE, VerificationStatus.PENDING)
    if account is None or account.verification_status not in open_statuses:
        item.decision = 'IGNORED'
        return
    if result['outcome'] == tips_message.EMPTY:
        if account.verification_status == VerificationStatus.PENDING_MUSE:
            account.verification_status = item.previous_status
            account.save(user=inbound_audit_user())
        item.decision = 'UNCHECKED'
        return
    payload = {'account_uuid': str(account.uuid), 'muse_reference': ref, 'returned_name': result['full_name'],
               'raw_response': {'channel': 'TIPS', **result}}
    if result['outcome'] != tips_message.FOUND:
        payload['result'] = 'FAILED'
        payload['failure_reason'] = (f"TIPS {result['error_code'] or '(no code)'}: "
                                     f"{result['error_description'] or 'account could not be found'}")
    applied = MuseVerificationInboundService().handle_result(payload)
    if not applied.get('success'):
        raise RuntimeError(applied.get('error') or applied.get('detail') or 'result not applied')
    account.refresh_from_db()
    item.decision = VerificationStatus(account.verification_status).name[:10]


def apply_results(msg_ids, results, raw):
    from tasaf_payment.models import GepgItemStatus, TipsLookupItem, TipsLookupRequest, TipsRequestStatus

    if isinstance(msg_ids, str):
        msg_ids = [msg_ids]
    keys = {_key(r['identifier_type'], r['identifier'], r['fsp_code']) for r in results if r['identifier']}
    identifiers = {k[0] for k in keys} | {re.sub(r'^255', '0', k[0]) for k in keys}
    counts = {'applied': 0, 'duplicate': 0, 'unmatched': 0}
    with transaction.atomic():
        ours = [m for m in msg_ids or [] if m]
        requests = TipsLookupRequest.objects.select_for_update().filter(msg_id__in=ours) if ours else None
        if not requests or not requests.exists():
            requests = TipsLookupRequest.objects.select_for_update().filter(status__in=MATCHABLE)
        request_ids = list(requests.values_list('id', flat=True))
        items = {}
        for item in (TipsLookupItem.objects.filter(request_id__in=request_ids, identifier__in=identifiers)
                     .select_related('payment_account', 'request').order_by('-request__created_at', 'id')):
            items.setdefault(_key(item.identifier_type, item.identifier, item.fsp_code), []).append(item)
        touched = set()
        for result in results:
            key = _key(result['identifier_type'], result['identifier'], result['fsp_code'])
            candidates = items.get(key) or []
            if not candidates and result['fsp_code'] == '':
                candidates = next((v for k, v in items.items() if k[0] == key[0]), [])
            waiting = [i for i in candidates if i.status == GepgItemStatus.PENDING]
            if not waiting:
                counts['duplicate' if candidates else 'unmatched'] += 1
                continue
            item = waiting[0]
            _apply_item(item, result, item.request.msg_id)
            item.save()
            touched.add(item.request_id)
            counts['applied'] += 1
        now = datetime.now(tz=timezone.utc)
        jobs = set()
        for request in TipsLookupRequest.objects.filter(id__in=touched):
            request.status = TipsRequestStatus.ANSWERED
            request.status_desc = answered_desc(request.items.values_list('status', 'decision'))
            request.answered_at = now
            request.response_body = tips_message.without_identity(raw)
            request.save(update_fields=['status', 'status_desc', 'answered_at', 'response_body'])
            if request.check_job_id:
                jobs.add(request.check_job_id)
            else:
                from tasaf_payment.events import emit_tips_answered
                emit_tips_answered(request, dict(Counter(
                    d or 'PENDING' for d in request.items.values_list('decision', flat=True))))
        if jobs:
            from tasaf_payment.account_check import complete
            for job_id in jobs:
                complete(job_id)
    logger.info("TIPS results: %(applied)d applied, %(duplicate)d duplicate, %(unmatched)d unmatched", counts)
    return counts


def handle(payload):
    message = payload.get('message') if isinstance(payload, dict) else None
    header = (message or {}).get('messageHeader') if isinstance(message, dict) else None
    if not isinstance(header, dict):
        return False, 'Not a TIPS message: no message.messageHeader'
    results = tips_message.results_in(payload)
    if not results:
        return True, tips_message.ack(header, False, 'No lookup results in the message')
    summary = message.get('messageSummary') or {}
    try:
        counts = apply_results([summary.get('orgMsgId'), summary.get('request-id'), header.get('msgId')],
                               results, payload)
    except Exception as exc:  # noqa: BLE001 — not acknowledged, so TIPS sends it again
        logger.exception("TIPS inbound %s failed", header.get('msgId'))
        return False, f'Could not process the results: {exc}'
    if not counts['applied'] and not counts['duplicate']:
        return True, tips_message.ack(header, False, f"No matching lookup for {counts['unmatched']} result(s)")
    return True, tips_message.ack(header)
