from datetime import datetime, timedelta, timezone

from django.db import transaction
from django.db.models import Q

RELEASABLE = ('SENDING', 'SENT', 'ACCEPTED')


def overdue(hours):
    from tasaf_payment.models import TipsLookupRequest, TipsRequestStatus
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    return TipsLookupRequest.objects.filter(
        Q(status__in=(TipsRequestStatus.SENT, TipsRequestStatus.ACCEPTED), sent_at__lt=cutoff)
        | Q(status=TipsRequestStatus.SENDING, created_at__lt=cutoff),
    ).order_by('created_at')


def release_request(request, reason, user):
    from tasaf_payment.models import GepgItemStatus, TipsRequestStatus
    from tasaf_payment.tips.sender import release_items

    with transaction.atomic():
        pending = list(request.items.filter(status=GepgItemStatus.PENDING).select_related('request'))
        release_items(pending, reason, user)
        request.items.filter(id__in=[i.id for i in pending]).update(status=GepgItemStatus.EXPIRED)
        request.status = TipsRequestStatus.EXPIRED
        request.status_desc = reason
        request.save(update_fields=['status', 'status_desc'])
    return len(pending)


def release(msg_id, user):
    from tasaf_payment.models import TipsLookupRequest

    with transaction.atomic():
        request = TipsLookupRequest.objects.select_for_update().filter(msg_id=msg_id).first()
        if not request:
            raise ValueError(f'No TIPS lookup {msg_id}')
        if request.status not in RELEASABLE:
            raise ValueError(f'Lookup {msg_id} is {request.status}; only a lookup still waiting for TIPS '
                             f'can be released')
        return release_request(request, f'Released by {user.username} to send again', user)


def expire(hours, apply=False):
    from tasaf_payment.models import GepgItemStatus
    from tasaf_payment.services import inbound_audit_user

    rows = []
    for request in overdue(hours):
        rows.append({'msg_id': request.msg_id, 'fsp_code': request.fsp_code,
                     'sent_at': request.sent_at.isoformat() if request.sent_at else None,
                     'accounts': request.items.filter(status=GepgItemStatus.PENDING).count()})
        if apply:
            release_request(request, f'No TIPS answer within {hours} hours', inbound_audit_user())
    return rows
