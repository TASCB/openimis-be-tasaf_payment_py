import logging
from datetime import datetime, timezone

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from tasaf_payment.models import (
    UNAPPLIED_ACTION_KEY, PaylistItem, PaylistItemStatus, PreAuditStatus, UnappliedAction, VerificationStatus,
    awaiting_action, returned_for_repayment,
)

logger = logging.getLogger(__name__)

REPAID, LAPSED, REPAYING, IN_CORRECTION = 'REPAID', 'LAPSED', 'REPAYING', 'IN_CORRECTION'
AWAITING_ACTION, UNAPPLIED_WITHDRAWN = 'AWAITING_ACTION', 'UNAPPLIED_WITHDRAWN'
RESOLUTIONS = (AWAITING_ACTION, IN_CORRECTION, REPAYING, REPAID, UNAPPLIED_WITHDRAWN, LAPSED)
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


def _withdrawn_by_pct():
    # has_key first: ~ on a missing key is NULL and would drop the row.
    return (Q(json_ext__has_key=UNAPPLIED_ACTION_KEY)
            & Q(**{f'json_ext__{UNAPPLIED_ACTION_KEY}__action': UnappliedAction.WITHDRAW}))


def resolution_q(resolution):
    if resolution == AWAITING_ACTION:
        return awaiting_action()
    base = returned_for_repayment()
    steps = [(REPAID, _paid()), (LAPSED, _lapsed()), (UNAPPLIED_WITHDRAWN, _withdrawn_by_pct()),
             (REPAYING, Q(_repaying()) | _ready())]
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
    if item.status == PaylistItemStatus.UNAPPLIED and UNAPPLIED_ACTION_KEY not in ext:
        return AWAITING_ACTION
    other = repayment_item(item) if item.benefit_consumption_id else None
    if other is not None and other.status == PaylistItemStatus.PROCESSED:
        return REPAID
    if LAPSED_KEY in ext:
        return LAPSED
    if (ext.get(UNAPPLIED_ACTION_KEY) or {}).get('action') == UnappliedAction.WITHDRAW:
        return UNAPPLIED_WITHDRAWN
    if other is not None or _account_cleared(item.payment_account):
        return REPAYING
    return IN_CORRECTION


# ── The PCT's decision on an unapplied payment ─────────────────────────────

class UnappliedDecisionError(Exception):
    pass


def latest_reason(item):
    feedback = item.return_feedbacks.filter(is_deleted=False).order_by('-received_at').first()
    parts = [feedback.reason_code, feedback.reason_description] if feedback else [item.return_reason]
    return ' '.join(p for p in parts if p) or 'Payment returned unapplied'


def suggested_action(item):
    from tasaf_payment.muse.rejection import DATA, TRANSIENT, classify
    feedback = item.return_feedbacks.filter(is_deleted=False).order_by('-received_at').first()
    reason = (feedback.reason_description if feedback else None) or item.return_reason
    return {TRANSIENT: UnappliedAction.REPAY, DATA: UnappliedAction.CASE}.get(classify(reason))


def _account_cleared(account):
    return (account is not None and not account.is_deleted
            and account.verification_status == VerificationStatus.VERIFIED
            and account.pre_audit_status == PreAuditStatus.PASSED)


def decide_unapplied(user, item_uuid, action, note=None):
    from tasaf_payment.muse.rejection import flag_returned_account
    from tasaf_payment.unapplied import provider_code

    action = str(action or '').strip().upper()
    if action not in UnappliedAction.values:
        raise UnappliedDecisionError(f"Unknown action {action!r}")
    with transaction.atomic():
        item = (PaylistItem.objects.select_for_update().select_related('paylist', 'payment_account')
                .filter(uuid=item_uuid, is_deleted=False).first())
        if item is None:
            raise UnappliedDecisionError("Payment not found")
        if not PaylistItem.objects.filter(awaiting_action(), id=item.id).exists():
            raise UnappliedDecisionError("This payment is not waiting for a decision")
        account = item.payment_account
        if action == UnappliedAction.REPAY and not _account_cleared(account):
            raise UnappliedDecisionError(
                "The account is not verified and pre-audited, so it cannot be repaid as it is; send it to Case")

        reason = latest_reason(item)
        item.json_ext = {**(item.json_ext or {}), UNAPPLIED_ACTION_KEY: {
            'action': action, 'by': user.username, 'at': datetime.now(tz=timezone.utc).isoformat(),
            'note': (note or '').strip() or None, 'suggested': suggested_action(item),
            'provider': provider_code(account),
        }}
        item.save(user=user)

        if action == UnappliedAction.CASE:
            flag_returned_account(item, reason, 'UNAPPLIED', item.paylist, user)
        elif action == UnappliedAction.WITHDRAW and account is not None:
            account.verification_status = VerificationStatus.PENDING
            account.pre_audit_status = PreAuditStatus.PENDING
            account.json_ext = {**(account.json_ext or {}), 'muse_unapplied': {
                'reason': reason, 'class': 'UNAPPLIED_WITHDRAWN', 'batch': item.paylist.muse_batch_reference,
                'msg_id': item.paylist.muse_msg_id}}
            account.save(user=user)
    logger.info("Unapplied payment %s: %s by %s", item_uuid, action, user.username)
    return item
