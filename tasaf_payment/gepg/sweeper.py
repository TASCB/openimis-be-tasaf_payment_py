from datetime import datetime, timedelta, timezone

from django.db import transaction


def overdue(hours=0, minutes=0):
    from tasaf_payment.models import GepgRequestStatus, GepgVerificationRequest
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours, minutes=minutes)
    return GepgVerificationRequest.objects.filter(
        status__in=(GepgRequestStatus.SENT, GepgRequestStatus.ACCEPTED), sent_at__lt=cutoff,
    ).order_by('sent_at')


def remind(minutes, apply=False):
    """Requests GePG has not answered within `minutes`; each one is announced once."""
    from tasaf_payment.events import emit_gepg_unanswered

    rows = []
    for request in overdue(minutes=minutes).filter(reminded_at__isnull=True, check_job__isnull=True):
        rows.append({'acc_verif_id': request.acc_verif_id, 'fsp_code': request.fsp_code,
                     'sent_at': request.sent_at.isoformat() if request.sent_at else None})
        if apply:
            with transaction.atomic():
                request.reminded_at = datetime.now(tz=timezone.utc)
                request.save(update_fields=['reminded_at'])
                emit_gepg_unanswered(request, minutes)
    return rows


RELEASABLE = ('SENDING', 'SENT', 'ACCEPTED')


def release_request(request, reason, user):
    from tasaf_payment.gepg.sender import _release
    from tasaf_payment.models import GepgItemStatus, GepgRequestStatus

    with transaction.atomic():
        pending = list(request.items.filter(status=GepgItemStatus.PENDING).select_related('request'))
        _release(pending, reason, user)
        request.items.filter(status=GepgItemStatus.RELEASED, id__in=[i.id for i in pending]) \
            .update(status=GepgItemStatus.EXPIRED)
        request.status = GepgRequestStatus.EXPIRED
        request.status_desc = reason
        request.save(update_fields=['status', 'status_desc'])
        if request.check_job_id:
            from tasaf_payment.account_check import complete
            complete(request.check_job_id)
    return len(pending)


def release(acc_verif_id, user):
    from tasaf_payment.models import GepgVerificationRequest

    with transaction.atomic():
        request = GepgVerificationRequest.objects.select_for_update().filter(acc_verif_id=acc_verif_id).first()
        if not request:
            raise ValueError(f'No GePG verification request {acc_verif_id}')
        if request.status not in RELEASABLE:
            raise ValueError(f'Request {acc_verif_id} is {request.status}; only a request still waiting for '
                             f'GePG can be released')
        return release_request(request, f'Released by {user.username} to send again', user)


def expire(hours, apply=False):
    from tasaf_payment.models import GepgItemStatus
    from tasaf_payment.services import _inbound_audit_user

    rows = []
    for request in overdue(hours=hours):
        rows.append({'acc_verif_id': request.acc_verif_id, 'fsp_code': request.fsp_code,
                     'sent_at': request.sent_at.isoformat() if request.sent_at else None,
                     'accounts': request.items.filter(status=GepgItemStatus.PENDING).count()})
        if apply:
            release_request(request, f'No GePG answer within {hours} hours', _inbound_audit_user())
    return rows
