from django.db.models import Exists, OuterRef, Q

from tasaf_payment.models import (
    PaylistItem, PaylistItemStatus, PreAuditStatus, VerificationStatus, returned_for_repayment,
)

REPAID, LAPSED, REPAYING, READY, IN_CORRECTION, FINAL = (
    'REPAID', 'LAPSED', 'REPAYING', 'READY', 'IN_CORRECTION', 'FINAL')
RESOLUTIONS = (IN_CORRECTION, READY, REPAYING, REPAID, LAPSED, FINAL)
FINISHED_PAYLISTS = ('CLOSED', 'REJECTED_AT_APPROVAL')


def _other_items(status_q):
    return PaylistItem.objects.filter(
        status_q, is_deleted=False, paylist__is_deleted=False,
        benefit_consumption_id=OuterRef('benefit_consumption_id'),
    ).exclude(id=OuterRef('id'))


def _paid():
    return Exists(_other_items(Q(status=PaylistItemStatus.PROCESSED)))


def _repaying():
    return Exists(_other_items(Q(status=PaylistItemStatus.PENDING)).exclude(paylist__status__in=FINISHED_PAYLISTS))


def _lapsed():
    from tasaf_payment.withdrawn_hold import LAPSED_KEY
    return Q(json_ext__has_key=LAPSED_KEY)


def _ready():
    return Q(payment_account__verification_status=VerificationStatus.VERIFIED,
             payment_account__pre_audit_status=PreAuditStatus.PASSED, payment_account__is_deleted=False)


def resolution_q(resolution):
    final = Q(status=PaylistItemStatus.UNAPPLIED) & ~Q(json_ext__has_key='returned_at')
    if resolution == FINAL:
        return final
    base = returned_for_repayment()
    steps = [(REPAID, _paid()), (LAPSED, _lapsed()), (REPAYING, _repaying()), (READY, _ready())]
    earlier = []
    for name, condition in steps:
        condition = condition if isinstance(condition, Q) else Q(condition)
        if name == resolution:
            return _without(base & condition, earlier)
        earlier.append(condition)
    if resolution == IN_CORRECTION:
        return _without(base, earlier)
    return Q(pk__in=[])


def _without(q, earlier):
    for condition in earlier:
        q &= ~condition
    return q


def repayment_item(item):
    others = PaylistItem.objects.filter(
        is_deleted=False, paylist__is_deleted=False, benefit_consumption_id=item.benefit_consumption_id,
    ).exclude(id=item.id).select_related('paylist')
    return (others.filter(status=PaylistItemStatus.PROCESSED).first()
            or others.filter(status=PaylistItemStatus.PENDING).exclude(paylist__status__in=FINISHED_PAYLISTS)
            .order_by('-date_created').first())


def resolution_of(item):
    from tasaf_payment.withdrawn_hold import LAPSED_KEY
    if item.status not in (PaylistItemStatus.WITHDRAWN, PaylistItemStatus.UNAPPLIED):
        return None
    ext = item.json_ext or {}
    if item.status == PaylistItemStatus.UNAPPLIED and 'returned_at' not in ext:
        return FINAL
    other = repayment_item(item) if item.benefit_consumption_id else None
    if other is not None and other.status == PaylistItemStatus.PROCESSED:
        return REPAID
    if LAPSED_KEY in ext:
        return LAPSED
    if other is not None:
        return REPAYING
    account = item.payment_account
    if (account and not account.is_deleted and account.verification_status == VerificationStatus.VERIFIED
            and account.pre_audit_status == PreAuditStatus.PASSED):
        return READY
    return IN_CORRECTION
