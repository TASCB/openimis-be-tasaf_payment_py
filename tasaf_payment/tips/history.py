from django.db.models import Count, F, Q


def _iso(value):
    return value.isoformat() if value else None


def request_rows(status=None, limit=50, fsp=None):
    from tasaf_payment.models import GepgItemStatus, TipsLookupRequest
    qs = TipsLookupRequest.objects.annotate(
        answered=Count('items', filter=Q(items__status=GepgItemStatus.ANSWERED)),
        verified=Count('items', filter=Q(items__decision='VERIFIED')),
        failed=Count('items', filter=Q(items__decision='FAILED')),
        manual=Count('items', filter=Q(items__decision='MANUAL')),
        unchecked=Count('items', filter=Q(items__decision='UNCHECKED')),
    )
    if status:
        qs = qs.filter(status=status)
    if fsp:
        qs = qs.filter(fsp_code=fsp)
    qs = qs.defer('request_body', 'ack_body', 'response_body')
    return [{
        'msg_id': r.msg_id, 'kind': r.kind, 'fsp_code': r.fsp_code, 'tips_code': r.tips_code, 'status': r.status,
        'status_desc': r.status_desc, 'item_count': r.item_count, 'answered': r.answered, 'verified': r.verified,
        'failed': r.failed, 'manual': r.manual, 'unchecked': r.unchecked, 'attempts': r.attempts,
        'requested_by': r.requested_by, 'esb_request_id': r.esb_request_id,
        'created_at': _iso(r.created_at), 'sent_at': _iso(r.sent_at), 'answered_at': _iso(r.answered_at),
    } for r in qs.order_by('-created_at')[:min(int(limit), 500)]]


ITEM_FIELDS = ('identifier_type', 'identifier', 'fsp_code', 'expected_name', 'full_name', 'status', 'error_code',
               'error_description', 'decision', 'answered_at')


def item_rows(msg_id, offset=0, limit=100, search=''):
    from tasaf_payment.models import TipsLookupItem
    qs = TipsLookupItem.objects.filter(request__msg_id=msg_id)
    search = (search or '').strip()
    if search:
        qs = qs.filter(Q(identifier__icontains=search) | Q(expected_name__icontains=search)
                       | Q(full_name__icontains=search)
                       | Q(payment_account__group_beneficiary__group__code__icontains=search))
    qs = qs.order_by('id').annotate(hhid=F('payment_account__group_beneficiary__group__code'))
    offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 100), 500))
    rows = list(qs.values(*ITEM_FIELDS, 'hhid')[offset:offset + limit])
    for row in rows:
        row['answered_at'] = _iso(row['answered_at'])
    return {'total': qs.count(), 'offset': offset, 'rows': rows}
