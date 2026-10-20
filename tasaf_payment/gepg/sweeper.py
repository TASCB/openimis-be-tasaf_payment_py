from datetime import datetime, timedelta, timezone

from django.db import transaction


def overdue(hours):
    from tasaf_payment.models import GepgRequestStatus, GepgVerificationRequest
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    return GepgVerificationRequest.objects.filter(
        status__in=(GepgRequestStatus.SENT, GepgRequestStatus.ACCEPTED), sent_at__lt=cutoff,
    ).order_by('sent_at')


def expire(hours, apply=False):
    from tasaf_payment.gepg.sender import _release
    from tasaf_payment.models import GepgItemStatus, GepgRequestStatus
    from tasaf_payment.services import _inbound_audit_user

    rows = []
    for request in overdue(hours):
        pending = list(request.items.filter(status=GepgItemStatus.PENDING).select_related('request'))
        rows.append({'acc_verif_id': request.acc_verif_id, 'fsp_code': request.fsp_code,
                     'sent_at': request.sent_at.isoformat() if request.sent_at else None,
                     'accounts': len(pending)})
        if not apply:
            continue
        reason = f'No GePG answer within {hours} hours'
        with transaction.atomic():
            _release(pending, reason, _inbound_audit_user())
            request.items.filter(status=GepgItemStatus.RELEASED, id__in=[i.id for i in pending]) \
                .update(status=GepgItemStatus.EXPIRED)
            request.status = GepgRequestStatus.EXPIRED
            request.status_desc = reason
            request.save(update_fields=['status', 'status_desc'])
    return rows
