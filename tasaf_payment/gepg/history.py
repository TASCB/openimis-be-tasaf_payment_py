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
        unchecked=Count('items', filter=Q(items__decision='UNCHECKED')),
    )
    if status:
        qs = qs.filter(status=status)
    return [{
        'acc_verif_id': r.acc_verif_id, 'fsp_code': r.fsp_code, 'bic': r.bic, 'status': r.status,
        'status_code': r.status_code, 'status_desc': r.status_desc, 'item_count': r.item_count,
        'answered': r.answered, 'verified': r.verified, 'failed': r.failed, 'manual': r.manual,
        'unchecked': r.unchecked,
        'attempts': r.attempts, 'requested_by': r.requested_by, 'after_change': r.after_change,
        'esb_request_id': r.esb_request_id,
        'created_at': _iso(r.created_at), 'sent_at': _iso(r.sent_at), 'answered_at': _iso(r.answered_at),
    } for r in qs.order_by('-created_at')[:min(int(limit), 500)]]


ITEM_FIELDS = ('item_id', 'acc_num', 'expected_name', 'acc_name', 'status', 'acc_sts_code', 'acc_sts_desc',
               'decision', 'answered_at')


def item_rows(acc_verif_id, offset=0, limit=100, search='', decision=''):
    from django.db.models import F
    from tasaf_payment.models import GepgVerificationItem
    qs = GepgVerificationItem.objects.filter(request__acc_verif_id=acc_verif_id)
    search = (search or '').strip()
    if search:
        qs = qs.filter(Q(acc_num__icontains=search) | Q(expected_name__icontains=search)
                       | Q(acc_name__icontains=search)
                       | Q(payment_account__group_beneficiary__group__code__icontains=search))
    if decision == 'NONE':
        qs = qs.filter(decision='')
    elif decision:
        qs = qs.filter(decision=decision)
    offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 100), 500))
    rows = list(qs.order_by('id')
                .annotate(hhid=F('payment_account__group_beneficiary__group__code'))
                .values(*ITEM_FIELDS, 'hhid')[offset:offset + limit])
    for row in rows:
        row['answered_at'] = _iso(row['answered_at'])
    return {'total': qs.count(), 'offset': offset, 'rows': rows}
