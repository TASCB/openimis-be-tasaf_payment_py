import logging
from datetime import datetime, timezone

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Sum

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


def times_returned(item):
    if not item.benefit_consumption_id:
        return 1
    return PaylistItem.objects.filter(benefit_consumption_id=item.benefit_consumption_id, is_deleted=False,
                                      status=PaylistItemStatus.UNAPPLIED).count()


def _check_decision(user, item, action, note):
    from tasaf_payment.apps import TasafPaymentConfig
    suggested = suggested_action(item)
    if action == UnappliedAction.REPAY:
        if not _account_cleared(item.payment_account):
            raise UnappliedDecisionError(
                "The account is not verified and pre-audited, so it cannot be repaid as it is; send it to Case")
        limit = int(getattr(TasafPaymentConfig, 'unapplied_max_returns', 0) or 0)
        returns = times_returned(item)
        if limit and returns >= limit:
            raise UnappliedDecisionError(
                f"This payment came back unapplied {returns} times, so it cannot be repaid as it is; "
                f"send it to Case or withdraw it")
        if suggested == UnappliedAction.CASE and not user.has_perms(
                TasafPaymentConfig.gql_unapplied_manage_perms or []):
            raise UnappliedDecisionError(
                "MUSE's reason points to a problem with the account; only a PCT Manager can repay it as it is")
    if not note and (action == UnappliedAction.WITHDRAW or (suggested and action != suggested)):
        raise UnappliedDecisionError(
            "Write a note: it is required to withdraw a payment or to choose another action than the suggested one")


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
        note = (note or '').strip()
        _check_decision(user, item, action, note)

        reason = latest_reason(item)
        item.json_ext = {**(item.json_ext or {}), UNAPPLIED_ACTION_KEY: {
            'action': action, 'by': user.username, 'at': datetime.now(tz=timezone.utc).isoformat(),
            'note': note or None, 'suggested': suggested_action(item), 'returns': times_returned(item),
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


# ── Where the returned money is (reconciliation) ───────────────────────────

def reconciliation(payment_cycle_id=None):
    """Per payment cycle and provider: returned = re-paid + withdrawn + lapsed + still held."""
    base = PaylistItem.objects.filter(status=PaylistItemStatus.UNAPPLIED, is_deleted=False, paylist__is_deleted=False)
    if payment_cycle_id:
        base = base.filter(paylist__payment_cycle_id=payment_cycle_id)
    keys = ('paylist__payment_cycle__code', 'payment_account__fsp_name')
    rows = {}

    def add(qs, name):
        for r in qs.values(*keys).annotate(n=Count('id'), amount=Sum('amount')):
            row = rows.setdefault((r[keys[0]], r[keys[1]]), {'cycle': r[keys[0]], 'fsp': r[keys[1]]})
            row[name] = {'count': r['n'], 'amount': str(r['amount'] or 0)}

    add(base, 'returned')
    for resolution in RESOLUTIONS:
        add(base.filter(resolution_q(resolution)), resolution)
    return sorted(rows.values(), key=lambda r: (r['cycle'] or '', r['fsp'] or ''))


# ── Undecided for too long (ageing) ────────────────────────────────────────

REMINDED_KEY = 'unapplied_reminded_at'


def remind_overdue(days, apply=False):
    """Announces each payment undecided after `days` once."""
    from datetime import timedelta
    from tasaf_payment.events import emit_muse_event

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    items = [i for i in PaylistItem.objects.filter(awaiting_action(), is_deleted=False, paylist__is_deleted=False)
             .exclude(json_ext__has_key=REMINDED_KEY).select_related('paylist')
             if (_returned_at(i) or cutoff) < cutoff]
    by_paylist = {}
    for item in items:
        by_paylist.setdefault(item.paylist_id, []).append(item)
    if apply:
        stamp = datetime.now(tz=timezone.utc).isoformat()
        for paylist_items in by_paylist.values():
            with transaction.atomic():
                for item in paylist_items:
                    PaylistItem.objects.filter(id=item.id).update(
                        json_ext={**(item.json_ext or {}), REMINDED_KEY: stamp})
                emit_muse_event(paylist_items[0].paylist, 'unapplied_overdue', count=len(paylist_items), days=days)
    return [{'paylist': p[0].paylist.muse_batch_reference or str(p[0].paylist_id), 'count': len(p)}
            for p in by_paylist.values()]


def _returned_at(item):
    ext = item.json_ext or {}
    if ext.get('returned_at'):
        when = datetime.fromisoformat(ext['returned_at'])
    else:
        feedback = item.return_feedbacks.filter(is_deleted=False).order_by('-received_at').first()
        when = feedback.received_at if feedback else None
    # openIMIS stores naive UTC (USE_TZ off)
    return when.replace(tzinfo=timezone.utc) if when is not None and when.tzinfo is None else when
