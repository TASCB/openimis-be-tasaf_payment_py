import logging
import re
from collections import namedtuple
from datetime import datetime, timezone

from django.db import transaction

from tasaf_payment.muse import message as muse_message
from tasaf_payment.msisdn import normalise

logger = logging.getLogger(__name__)

DATA, TRANSIENT, UNKNOWN = 'DATA', 'TRANSIENT', 'UNKNOWN'
WITHDRAWN, KEPT, NEEDS_REVIEW = 'WITHDRAWN', 'KEPT', 'NEEDS_REVIEW'

ENTRY_RE = re.compile(
    r'\s*(?P<reason>.+?)\.?\s+for\s+(?P<account>(?=[0-9A-Za-z]*\d)[0-9A-Za-z]{6,35})\s*(?:,|$)')

Failure = namedtuple('Failure', 'reason account')


def parse(status_desc):
    text = (status_desc or '').strip()
    failures, pos = [], 0
    while pos < len(text):
        match = ENTRY_RE.match(text, pos)
        if not match:
            break
        failures.append(Failure(match['reason'].strip(), match['account']))
        pos = match.end()
    return failures, text[pos:].strip()


def _reason_key(reason):
    return re.sub(r'\s+', ' ', reason or '').strip().rstrip('.').lower()


def classify(reason, classes=None):
    if classes is None:
        from tasaf_payment.apps import TasafPaymentConfig
        classes = TasafPaymentConfig.muse_rejection_reason_classes or {}
    known = {_reason_key(k): str(v).upper() for k, v in classes.items()}
    return known.get(_reason_key(reason), UNKNOWN)


def _account_key(number):
    return normalise(str(number or '')).upper()


def _transient_limit():
    from tasaf_payment.apps import TasafPaymentConfig
    return max(1, int(TasafPaymentConfig.muse_transient_retry_limit or 1))


def decide(failure_class, transient_strikes, limit):
    if failure_class == TRANSIENT and transient_strikes < limit:
        return KEPT
    return WITHDRAWN


def handle_batch_rejection(paylist, status_desc):
    from tasaf_payment.models import PaylistItemStatus
    from tasaf_payment.services import _inbound_audit_user

    failures, leftover = parse(status_desc)
    user = _inbound_audit_user(paylist)
    pending = list(paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING)
                   .select_related('payment_account__group_beneficiary__group'))
    by_account = {}
    for item in pending:
        by_account.setdefault(_account_key(item.payment_account.account_number), []).append(item)

    rows, named = [], set()
    limit = _transient_limit()
    for failure in failures:
        failure_class = classify(failure.reason)
        matches = by_account.get(_account_key(failure.account), [])
        row = {'account': failure.account, 'reason': failure.reason, 'class': failure_class}
        if len(matches) != 1:
            row['action'] = NEEDS_REVIEW
            row['note'] = ('not a pending payee on this paylist' if not matches
                           else f'{len(matches)} payees on this paylist share this account')
            rows.append(row)
            continue
        item = matches[0]
        named.add(item.id)
        row['item'] = str(item.uuid)
        row['account_uuid'] = str(item.payment_account_id)
        strikes = int((item.json_ext or {}).get('muse_transient_strikes') or 0)
        if failure_class == TRANSIENT:
            strikes += 1
        row['action'] = decide(failure_class, strikes, limit)
        if row['action'] == KEPT:
            _set_strikes(item, strikes, user)
        else:
            row['follow_up'] = withdraw_item(item, failure.reason, failure_class, paylist, user)
        rows.append(row)

    for item in pending:
        if item.id not in named and (item.json_ext or {}).get('muse_transient_strikes'):
            _set_strikes(item, 0, user)

    record = {
        'attempt': muse_message.attempt(paylist),
        'msg_id': paylist.muse_msg_id,
        'at': datetime.now(tz=timezone.utc).isoformat(),
        'status_desc': status_desc,
        'unparsed': leftover or None,
        'payees': rows,
        'withdrawn': sum(1 for r in rows if r['action'] == WITHDRAWN),
        'kept': sum(1 for r in rows if r['action'] == KEPT),
        'needs_review': sum(1 for r in rows if r['action'] == NEEDS_REVIEW) + (1 if leftover else 0),
    }
    ext = dict(paylist.json_ext or {})
    ext['muse_rejections'] = [*(ext.get('muse_rejections') or []), record]
    paylist.json_ext = ext
    paylist.save(user=user)

    from tasaf_payment.events import emit_muse_event
    emit_muse_event(paylist, 'payees_withdrawn', withdrawn=record['withdrawn'], kept=record['kept'],
                    needs_review=record['needs_review'], description=status_desc)
    logger.info("MUSE rejection of paylist %s: %d withdrawn, %d kept, %d need review",
                paylist.uuid, record['withdrawn'], record['kept'], record['needs_review'])
    return record


def _set_strikes(item, strikes, user):
    ext = dict(item.json_ext or {})
    if strikes:
        ext['muse_transient_strikes'] = strikes
    else:
        ext.pop('muse_transient_strikes', None)
    item.json_ext = ext
    item.save(user=user)


def withdraw_item(item, reason, failure_class, paylist, user):
    from tasaf_payment.models import PaylistItemStatus, ReturnFeedback, ReturnFeedbackType

    ReturnFeedback(paylist_item=item, feedback_type=ReturnFeedbackType.REJECTED_IN_BATCH,
                   reason_code=failure_class, reason_description=reason).save(user=user)
    item.status = PaylistItemStatus.WITHDRAWN
    item.return_reason = reason
    ext = dict(item.json_ext or {})
    ext.pop('muse_transient_strikes', None)
    ext['withdrawn_at'] = datetime.now(tz=timezone.utc).isoformat()
    item.json_ext = ext
    item.save(user=user)
    return flag_returned_account(item, reason, failure_class, paylist, user)


def flag_returned_account(item, reason, failure_class, paylist, user):
    from tasaf_payment.models import VerificationStatus

    account = item.payment_account
    account.verification_status = (VerificationStatus.MANUAL if failure_class in (TRANSIENT, UNKNOWN)
                                   else VerificationStatus.FAILED)
    account_ext = dict(account.json_ext or {})
    key = 'muse_unapplied' if failure_class == 'UNAPPLIED' else 'muse_rejection'
    account_ext[key] = {'reason': reason, 'class': failure_class,
                        'batch': paylist.muse_batch_reference, 'msg_id': paylist.muse_msg_id}
    account.json_ext = account_ext
    account.save(user=user)
    follow_up = _open_follow_up(item, reason, paylist, user, unapplied=failure_class == 'UNAPPLIED')
    if follow_up:
        item.json_ext = {**(item.json_ext or {}), 'follow_up_id': str(follow_up)}
        item.save(user=user)
    return follow_up


def _open_follow_up(item, reason, paylist, user, unapplied=False):
    try:
        from case_management.services import FollowUpService
    except ImportError:
        return None
    group = getattr(getattr(item.payment_account, 'group_beneficiary', None), 'group', None)
    if not group:
        logger.warning("Returned payee %s has no household; no follow-up opened", item.uuid)
        return None
    if unapplied:
        remark = (f"MUSE returned the payment on batch {paylist.muse_batch_reference} unapplied: "
                  f"{reason} for {item.payment_account.account_number}. "
                  f"Correct the payment details; it is then paid on a new paylist.")
    else:
        remark = (f"MUSE rejected payment batch {paylist.muse_batch_reference} "
                  f"(attempt {muse_message.attempt(paylist)}): {reason} for "
                  f"{item.payment_account.account_number}. The payment was taken out of the batch. "
                  f"Correct the payment details; it is then paid on a new paylist.")
    result = FollowUpService(user).add(group_id=group.id, category='PAYMENT_FAILURE', remark=remark,
                                       priority='HIGH', paylist_item_id=item.id)
    if not result.get('success'):
        logger.warning("No follow-up for returned payee %s: %s", item.uuid, result)
        return None
    return (result.get('data') or {}).get('id')
