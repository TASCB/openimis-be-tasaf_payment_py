from django.db.models import Count, Q


def _iso(value):
    return value.isoformat() if value else None


def request_rows(status=None, limit=50):
    from tasaf_payment.models import GepgItemStatus, GepgVerificationRequest
    qs = GepgVerificationRequest.objects.annotate(
        answered=Count('items', filter=Q(items__status=GepgItemStatus.ANSWERED)),
        verified=Count('items', filter=Q(items__decision='VERIFIED')),
        failed=Count('items', filter=Q(items__decision='FAILED')),
        manual=Count('items', filter=Q(items__decision='MANUAL')),
    )
    if status:
        qs = qs.filter(status=status)
    return [{
        'acc_verif_id': r.acc_verif_id, 'fsp_code': r.fsp_code, 'bic': r.bic, 'status': r.status,
        'status_code': r.status_code, 'status_desc': r.status_desc, 'item_count': r.item_count,
        'answered': r.answered, 'verified': r.verified, 'failed': r.failed, 'manual': r.manual,
        'attempts': r.attempts, 'requested_by': r.requested_by, 'after_change': r.after_change,
        'created_at': _iso(r.created_at), 'sent_at': _iso(r.sent_at), 'answered_at': _iso(r.answered_at),
    } for r in qs.order_by('-created_at')[:min(int(limit), 500)]]


def item_rows(acc_verif_id):
    from tasaf_payment.models import GepgVerificationItem
    qs = (GepgVerificationItem.objects.filter(request__acc_verif_id=acc_verif_id)
          .select_related('payment_account__group_beneficiary__group').order_by('id'))
    rows = []
    for i in qs:
        group = getattr(getattr(i.payment_account, 'group_beneficiary', None), 'group', None)
        rows.append({'item_id': i.item_id, 'hhid': getattr(group, 'code', None), 'acc_num': i.acc_num,
                     'expected_name': i.expected_name, 'acc_name': i.acc_name, 'status': i.status,
                     'acc_sts_code': i.acc_sts_code, 'acc_sts_desc': i.acc_sts_desc,
                     'decision': i.decision, 'answered_at': _iso(i.answered_at)})
    return rows
