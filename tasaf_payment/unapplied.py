import logging
from datetime import datetime, timezone

from django.db.models import Count

from tasaf_payment.models import (
    UNAPPLIED_ACTION_KEY, PaylistItem, PaylistItemStatus, UnappliedAction,
)

logger = logging.getLogger(__name__)

MAX_ID = 16
RESEND_ACTIONS = (UnappliedAction.REPAY, UnappliedAction.CASE)
PROVIDER_CHANGED = 'PROVIDER_CHANGED'


def provider_code(account):
    from tasaf_payment.charges import ChargeError, resolve_fsp_code
    if account is None:
        return None
    try:
        return resolve_fsp_code(account.fsp_name)
    except ChargeError:
        return (account.fsp_name or '').strip().upper() or None


# ── endToEndId ──────────────────────────────────────────────────────────────

def _sent_counts(benefit_ids):
    rows = (PaylistItem.objects.filter(benefit_consumption_id__in=benefit_ids, is_deleted=False,
                                       paylist__is_deleted=False, paylist__muse_msg_id__isnull=False)
            .exclude(paylist__muse_msg_id='')
            .values('benefit_consumption_id').annotate(n=Count('id')))
    return {r['benefit_consumption_id']: r['n'] for r in rows}


def _taken(candidates):
    from payroll.models import BenefitConsumption
    used = set(PaylistItem.objects.filter(end_to_end_id__in=candidates).values_list('end_to_end_id', flat=True))
    used |= set(BenefitConsumption.objects.filter(code__in=candidates).values_list('code', flat=True))
    return used


def new_id(code, attempt):
    suffix = f"R{attempt}"
    return f"{code[:MAX_ID - len(suffix)]}{suffix}"


def assign_end_to_end_ids(items):
    benefits = [i.benefit_consumption for i in items if i.benefit_consumption_id]
    sent = _sent_counts([b.id for b in benefits])
    wanted = {}
    for item in items:
        benefit = item.benefit_consumption
        if benefit is None:
            continue
        n = sent.get(benefit.id, 0)
        wanted[item] = (benefit.code, n + 1) if n else (benefit.code, None)
    taken = _taken([new_id(code, a) for code, a in wanted.values() if a])
    for item, (code, attempt) in wanted.items():
        if attempt is None:
            item.end_to_end_id = code
            continue
        candidate = new_id(code, attempt)
        while candidate in taken:
            attempt += 1
            candidate = new_id(code, attempt)
        taken.add(candidate)
        item.end_to_end_id = candidate


# ── Which payments are unapplied re-sends ───────────────────────────────────

def resend_origins(benefit_ids):
    items = (PaylistItem.objects.filter(benefit_consumption_id__in=benefit_ids, is_deleted=False,
                                        status=PaylistItemStatus.UNAPPLIED,
                                        **{f'json_ext__{UNAPPLIED_ACTION_KEY}__action__in': list(RESEND_ACTIONS)})
             .select_related('benefit_consumption', 'payment_account').order_by('date_created'))
    return {item.benefit_consumption_id: item for item in items}


def original_end_to_end_id(item):
    from tasaf_payment.muse.message import end_to_end_id
    return end_to_end_id(item)


# ── Provider rule ───────────────────────────────────────────────────────────

def provider_changed(item, account=None):
    decision = (item.json_ext or {}).get(UNAPPLIED_ACTION_KEY) or {}
    before = decision.get('provider')
    return bool(before) and provider_code(account or item.payment_account) != before


def apply_provider_change(account, user):
    changed = []
    items = PaylistItem.objects.filter(
        payment_account_id=account.id, is_deleted=False, status=PaylistItemStatus.UNAPPLIED,
        **{f'json_ext__{UNAPPLIED_ACTION_KEY}__action__in': list(RESEND_ACTIONS)})
    for item in items.select_related('payment_account'):
        if not provider_changed(item, account):
            continue
        ext = dict(item.json_ext or {})
        ext[UNAPPLIED_ACTION_KEY] = {**ext[UNAPPLIED_ACTION_KEY], 'action': UnappliedAction.WITHDRAW,
                                     'auto': PROVIDER_CHANGED, 'changed_at': datetime.now(tz=timezone.utc).isoformat(),
                                     'new_provider': provider_code(account)}
        item.json_ext = ext
        item.save(user=user)
        changed.append(item)
        logger.info("Unapplied payment %s withdrawn: provider changed to %s", item.uuid, provider_code(account))
    return changed


# ── Submit guard ────────────────────────────────────────────────────────────

def resend_mismatches(paylist):
    from tasaf_payment.muse.message import end_to_end_id
    items = list(paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING)
                 .select_related('payment_account', 'benefit_consumption'))
    originals = {}
    org_ids = {i.org_end_to_end_id for i in items if i.org_end_to_end_id}
    for original in (PaylistItem.objects.filter(status=PaylistItemStatus.UNAPPLIED, is_deleted=False)
                     .filter(benefit_consumption_id__in=[i.benefit_consumption_id for i in items])
                     .select_related('payment_account', 'benefit_consumption')):
        key = end_to_end_id(original)
        if key in org_ids:
            originals[key] = original
    bad = []
    for item in items:
        original = originals.get(item.org_end_to_end_id)
        if (original is None or item.amount != original.amount
                or provider_code(item.payment_account) != provider_code(original.payment_account)):
            bad.append(item)
    return bad
