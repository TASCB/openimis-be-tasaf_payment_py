import logging
from datetime import datetime, timezone

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Sum, Value

from tasaf_payment.models import (
    UNAPPLIED_ACTION_KEY, PaylistItem, PaylistItemStatus, PreAuditStatus, UnappliedAction, VerificationStatus,
    awaiting_action, returned_for_repayment,
)

logger = logging.getLogger(__name__)

REPAID, LAPSED, REPAYING, IN_CORRECTION = 'REPAID', 'LAPSED', 'REPAYING', 'IN_CORRECTION'
AWAITING_ACTION, UNAPPLIED_WITHDRAWN = 'AWAITING_ACTION', 'UNAPPLIED_WITHDRAWN'
RESOLUTIONS = (AWAITING_ACTION, IN_CORRECTION, REPAYING, REPAID, UNAPPLIED_WITHDRAWN, LAPSED)
HELD, HELD_RESOLUTIONS = 'HELD', (AWAITING_ACTION, IN_CORRECTION, REPAYING)
RECONCILIATION_PARTS = (('returned', 'Returned by MUSE'), (REPAID, 'Re-paid on a new paylist'),
                        (UNAPPLIED_WITHDRAWN, 'Withdrawn from MUSE'), (LAPSED, 'Lapsed'), (HELD, 'Still held'))
FINISHED_PAYLISTS = ('CLOSED', 'REJECTED_AT_APPROVAL')


def _other_items(status_q):
    return PaylistItem.objects.filter(
        status_q, is_deleted=False, paylist__is_deleted=False,
        benefit_consumption_id=OuterRef('benefit_consumption_id'),
    ).exclude(id=OuterRef('id'))


def paid_elsewhere_q():
    return Exists(_other_items(Q(status=PaylistItemStatus.PROCESSED)))


def repaying_q():
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
    if resolution == HELD:
        held = Q(pk__in=[])
        for name in HELD_RESOLUTIONS:
            held |= resolution_q(name)
        return held
    if resolution == AWAITING_ACTION:
        return awaiting_action()
    base = returned_for_repayment()
    steps = [(REPAID, paid_elsewhere_q()), (LAPSED, _lapsed()), (UNAPPLIED_WITHDRAWN, _withdrawn_by_pct()),
             (REPAYING, Q(repaying_q()) | _ready())]
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
    if other is not None or account_cleared(item.payment_account):
        return REPAYING
    return IN_CORRECTION


# ── The PCT's decision on an unapplied payment ─────────────────────────────

class UnappliedDecisionError(Exception):
    pass


def _latest_feedback(item):
    prefetched = getattr(item, '_prefetched_objects_cache', {}).get('return_feedbacks')
    if prefetched is not None:
        live = [f for f in prefetched if not f.is_deleted]
        return max(live, key=lambda f: f.received_at) if live else None
    return item.return_feedbacks.filter(is_deleted=False).order_by('-received_at').first()


def latest_reason(item):
    feedback = _latest_feedback(item)
    parts = [feedback.reason_code, feedback.reason_description] if feedback else [item.return_reason]
    return ' '.join(p for p in parts if p) or 'Payment returned unapplied'


def suggested_action(item):
    from tasaf_payment.muse.rejection import DATA, TRANSIENT, classify
    feedback = _latest_feedback(item)
    reason = (feedback.reason_description if feedback else None) or item.return_reason
    return {TRANSIENT: UnappliedAction.REPAY, DATA: UnappliedAction.CASE}.get(classify(reason))


def account_cleared(account):
    return (account is not None and not account.is_deleted
            and account.verification_status == VerificationStatus.VERIFIED
            and account.pre_audit_status == PreAuditStatus.PASSED)


def times_returned(item):
    if not item.benefit_consumption_id:
        return 1
    if getattr(item, 'returns_count', None):
        return item.returns_count
    return PaylistItem.objects.filter(benefit_consumption_id=item.benefit_consumption_id, is_deleted=False,
                                      status=PaylistItemStatus.UNAPPLIED).count()


def _check_decision(user, item, action, note):
    from tasaf_payment.apps import TasafPaymentConfig
    suggested = suggested_action(item)
    if action == UnappliedAction.REPAY:
        if not account_cleared(item.payment_account):
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


def decide_unapplied(user, item_uuid, action, note=None, batch=None):
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
            'provider': provider_code(account), **({'batch': batch} if batch else {}),
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

def reconciliation(**filters):
    """Per provider, under the Reports filters: returned = re-paid + withdrawn + lapsed + still held."""
    from tasaf_payment.reports import unapplied_items

    base = unapplied_items(**filters)
    zero = {'count': 0, 'amount': 0.0}
    rows = {}
    for key, _ in RECONCILIATION_PARTS:
        part = base if key == 'returned' else base.filter(resolution_q(key))
        for r in part.values('payment_account__fsp_name').annotate(n=Count('id'), amount=Sum('amount')):
            row = rows.setdefault(r['payment_account__fsp_name'], {'fsp': r['payment_account__fsp_name']})
            row[key] = {'count': r['n'], 'amount': float(r['amount'] or 0)}
    providers = [{**{key: zero for key, _ in RECONCILIATION_PARTS}, **row} for row in rows.values()]
    providers.sort(key=lambda row: (-row['returned']['amount'], row['fsp'] or ''))
    totals = {key: {'count': sum(row[key]['count'] for row in providers),
                    'amount': sum(row[key]['amount'] for row in providers)} for key, _ in RECONCILIATION_PARTS}
    parts = [key for key, _ in RECONCILIATION_PARTS[1:]]
    unexplained = {'count': totals['returned']['count'] - sum(totals[k]['count'] for k in parts),
                   'amount': round(totals['returned']['amount'] - sum(totals[k]['amount'] for k in parts), 2)}
    return {'providers': providers, 'totals': totals, 'unexplained': unexplained}


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
        feedback = _latest_feedback(item)
        when = feedback.received_at if feedback else None
    # openIMIS stores naive UTC (USE_TZ off)
    return when.replace(tzinfo=timezone.utc) if when is not None and when.tzinfo is None else when


# ── Filters of the awaiting list ───────────────────────────────────────────

SUGGESTION_CLASSES = {UnappliedAction.REPAY: 'TRANSIENT', UnappliedAction.CASE: 'DATA'}
NO_SUGGESTION = 'NONE'


def _with_reason():
    from django.db.models import Subquery, TextField
    from django.db.models.functions import Cast, Coalesce, NullIf
    from tasaf_payment.models import ReturnFeedback
    latest = ReturnFeedback.objects.filter(paylist_item_id=OuterRef('id'), is_deleted=False).order_by('-received_at')
    described = NullIf(Subquery(latest.values('reason_description')[:1], output_field=TextField()), Value(''))
    return (PaylistItem.objects.filter(status=PaylistItemStatus.UNAPPLIED, is_deleted=False)
            .annotate(reason=Coalesce(described, Cast('return_reason', TextField()), output_field=TextField())))


def suggested_q(value):
    from tasaf_payment.muse.rejection import classify
    value = str(value or '').strip().upper()
    items = _with_reason()
    reasons = {r for r in items.values_list('reason', flat=True).distinct() if r}
    if value == NO_SUGGESTION:
        known = [r for r in reasons if classify(r) in SUGGESTION_CLASSES.values()]
        return Q(id__in=items.filter(Q(reason__isnull=True) | ~Q(reason__in=known)).values('id'))
    wanted = SUGGESTION_CLASSES.get(value)
    if not wanted:
        return Q(pk__in=[])
    return Q(id__in=items.filter(reason__in=[r for r in reasons if classify(r) == wanted]).values('id'))


def reason_q(text):
    from tasaf_payment.models import ReturnFeedback
    text = str(text or '').strip()
    described = ReturnFeedback.objects.filter(paylist_item_id=OuterRef('id'), is_deleted=False,
                                              reason_description__icontains=text)
    return Q(return_reason__icontains=text) | Q(Exists(described))


def overdue_count(days=None):
    from datetime import timedelta
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.models import ReturnFeedback
    days = int(days if days is not None else getattr(TasafPaymentConfig, 'unapplied_reminder_days', 7) or 7)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    stamped = Q(json_ext__has_key='returned_at')
    recent = ReturnFeedback.objects.filter(paylist_item_id=OuterRef('id'), is_deleted=False,
                                           received_at__gte=cutoff.replace(tzinfo=None))
    any_feedback = ReturnFeedback.objects.filter(paylist_item_id=OuterRef('id'), is_deleted=False)
    old = (stamped & Q(json_ext__returned_at__lt=cutoff.isoformat())) | (
        ~stamped & Exists(any_feedback) & ~Exists(recent))
    return {'count': PaylistItem.objects.filter(awaiting_action(), old, is_deleted=False,
                                                paylist__is_deleted=False).count(), 'days': days}


# ── Deciding many at once ──────────────────────────────────────────────────

SUGGESTED = 'SUGGESTED'
BULK_OUTCOME_KEY = 'tasaf.unapplied.bulk:{}'
BULK_ACTIONS = (*UnappliedAction.values, SUGGESTED)
NO_SUGGESTION_REASON = "MUSE's reason is not in the reason table, so there is no suggested action"


def bulk_items(item_uuids=None, **filters):
    from tasaf_payment.schema import paylist_item_filters
    qs = PaylistItem.objects.filter(awaiting_action(), is_deleted=False, paylist__is_deleted=False)
    if item_uuids:
        return qs.filter(uuid__in=item_uuids)
    return qs.filter(*paylist_item_filters(**{k: v for k, v in filters.items() if k != 'resolution'}))


def _annotated(qs):
    from django.db.models import IntegerField, Subquery
    from django.db.models.functions import Coalesce
    same = (PaylistItem.objects.filter(benefit_consumption_id=OuterRef('benefit_consumption_id'), is_deleted=False,
                                       status=PaylistItemStatus.UNAPPLIED)
            .values('benefit_consumption_id').annotate(n=Count('id')).values('n'))
    return (qs.select_related('payment_account', 'paylist').prefetch_related('return_feedbacks')
            .annotate(returns_count=Coalesce(Subquery(same, output_field=IntegerField()), 1)))


def _effective(item, action):
    effective = suggested_action(item) if action == SUGGESTED else action
    return str(effective) if effective else None


def bulk_preview(user, action, note, items):
    from collections import Counter
    action = str(action or '').strip().upper()
    if action not in BULK_ACTIONS:
        raise UnappliedDecisionError(f"Unknown action {action!r}")
    note = (note or '').strip()
    decided, refused = Counter(), Counter()
    for item in _annotated(items).iterator(chunk_size=500):
        effective = _effective(item, action)
        if not effective:
            refused[NO_SUGGESTION_REASON] += 1
            continue
        try:
            _check_decision(user, item, effective, note)
            decided[effective] += 1
        except UnappliedDecisionError as exc:
            refused[str(exc)] += 1
    return {'action': action, 'total': sum(decided.values()) + sum(refused.values()),
            'decided': dict(decided), 'refused': [{'reason': r, 'count': n} for r, n in refused.most_common()]}


def bulk_decide(user, action, note, item_ids, batch):
    from collections import Counter
    decided, refused = Counter(), Counter()
    for item in _annotated(PaylistItem.objects.filter(id__in=item_ids)).iterator(chunk_size=500):
        effective = _effective(item, action)
        if not effective:
            refused[NO_SUGGESTION_REASON] += 1
            continue
        try:
            decide_unapplied(user, str(item.uuid), effective, note, batch=batch)
            decided[effective] += 1
        except UnappliedDecisionError as exc:
            refused[str(exc)] += 1
    result = {'batch': batch, 'action': action, 'total': len(item_ids), 'decided': dict(decided),
              'refused': [{'reason': r, 'count': n} for r, n in refused.most_common()]}
    logger.info("Unapplied bulk %s by %s: %s", batch, user.username, result)
    return result


def queue_bulk_decide(user, action, note, items):
    import uuid
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.tasks import decide_unapplied_bulk_task
    action = str(action or '').strip().upper()
    if action not in BULK_ACTIONS:
        raise UnappliedDecisionError(f"Unknown action {action!r}")
    item_ids = list(items.values_list('id', flat=True))
    if not item_ids:
        raise UnappliedDecisionError("No payment waiting for a decision matches")
    limit = int(getattr(TasafPaymentConfig, 'unapplied_bulk_max', 0) or 0)
    if limit and len(item_ids) > limit:
        raise UnappliedDecisionError(f"{len(item_ids)} payments match; narrow the filters to {limit} or fewer")
    batch, note = uuid.uuid4().hex[:12], (note or '').strip()
    if len(item_ids) > int(getattr(TasafPaymentConfig, 'unapplied_bulk_inline_max', 0) or 0):
        try:
            decide_unapplied_bulk_task.delay(user.id, action, note, item_ids, batch)
            return {'batch': batch, 'action': action, 'total': len(item_ids), 'queued': True}
        except Exception:  # noqa: BLE001 — no broker: decide inline
            logger.warning("Unapplied bulk: enqueue failed, deciding inline", exc_info=True)
    return bulk_decide(user, action, note, item_ids, batch)
