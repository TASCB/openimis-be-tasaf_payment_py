import logging
from datetime import datetime, timedelta, timezone

from django.db import transaction

logger = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3), 'EAT')
LAPSED_KEY = 'withdrawn_hold_lapsed'


def today_eat():
    return datetime.now(tz=EAT).date()


def _config(name, default):
    from tasaf_payment.apps import TasafPaymentConfig
    value = getattr(TasafPaymentConfig, name, None)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def withdrawn_on(item):
    stamped = (item.json_ext or {}).get('withdrawn_at')
    if stamped:
        try:
            return datetime.fromisoformat(stamped).astimezone(EAT).date()
        except ValueError:
            pass
    return item.date_updated.date() if item.date_updated else today_eat()


def deadline(payroll, item):
    cycle = getattr(payroll, 'payment_cycle', None)
    if cycle is not None and getattr(cycle, 'end_date', None):
        return cycle.end_date + timedelta(days=_config('withdrawn_hold_grace_days', 0))
    return withdrawn_on(item) + timedelta(days=_config('withdrawn_hold_days_without_cycle', 30))


def lapse_expired(payroll_id, today=None, apply=True):
    from payroll.models import Payroll
    from tasaf_payment.services import _inbound_audit_user, withdrawn_awaiting_payment

    today = today or today_eat()
    payroll = Payroll.objects.filter(id=payroll_id).select_related('payment_cycle').first()
    if payroll is None:
        return []
    due = [item for item in withdrawn_awaiting_payment(payroll_id).select_related('paylist')
           if today > deadline(payroll, item)]
    if not due or not apply:
        return due

    with transaction.atomic():
        for item in due:
            user = _inbound_audit_user(item)
            cut_off = deadline(payroll, item)
            ext = dict(item.json_ext or {})
            ext[LAPSED_KEY] = {'at': datetime.now(tz=timezone.utc).isoformat(), 'deadline': cut_off.isoformat()}
            item.json_ext = ext
            item.save(user=user)
            _escalate_follow_ups(item, cut_off, payroll, user)

        from tasaf_payment.events import emit_muse_event
        emit_muse_event(due[0].paylist, 'withdrawn_lapsed', lapsed=len(due))
    logger.info("Payroll %s: %d withdrawn payee(s) passed the cycle cut-off unpaid", payroll_id, len(due))
    return due


def _escalate_follow_ups(item, cut_off, payroll, user):
    try:
        from case_management.models import OPEN_FOLLOW_UP_STATUSES, FollowUpRemark, FollowUpStatus
        from case_management.services import FollowUpService
    except ImportError:
        return
    cycle = getattr(getattr(payroll, 'payment_cycle', None), 'code', None)
    note = (f"Not paid by the cycle cut-off ({cut_off:%d %b %Y}"
            f"{', cycle ' + cycle if cycle else ''}). The payment is left unpaid until the rule for "
            f"unpaid payments is decided; correcting the account still clears this follow-up.")
    for record in FollowUpRemark.objects.filter(paylist_item_id=item.id, category='PAYMENT_FAILURE',
                                                status__in=OPEN_FOLLOW_UP_STATUSES, is_deleted=False):
        record.status = FollowUpStatus.ESCALATED
        record.escalated_at = datetime.now(tz=timezone.utc)
        record.save(user=user)
        FollowUpService(user).add(group_id=record.group_id, category='PAYMENT_FAILURE', remark=note,
                                  priority='HIGH', parent_remark_id=record.id)


def held_payrolls():
    from tasaf_payment.models import PaylistItem, PaylistItemStatus
    return (PaylistItem.objects.filter(is_deleted=False, paylist__is_deleted=False,
                                       status=PaylistItemStatus.WITHDRAWN, paylist__payroll__isnull=False)
            .exclude(json_ext__has_key=LAPSED_KEY)
            .values_list('paylist__payroll_id', flat=True).distinct())


def release_expired_holds(today=None, apply=True):
    from tasaf_payment.models import Paylist, PaylistStatus
    from tasaf_payment.services import _notify_payroll_if_all_closed

    released = {}
    for payroll_id in list(held_payrolls()):
        lapsed = lapse_expired(payroll_id, today, apply=apply)
        if not lapsed:
            continue
        released[payroll_id] = lapsed
        if apply:
            closed = Paylist.objects.filter(payroll_id=payroll_id, is_deleted=False,
                                            status=PaylistStatus.CLOSED).first()
            if closed:
                _notify_payroll_if_all_closed(closed)
    return released


def scheduled_release():
    try:
        released = release_expired_holds()
        logger.info("Withdrawn-payee cut-off: %d payroll(s) released", len(released))
    except Exception:  # noqa: BLE001
        logger.exception("Withdrawn-payee cut-off job failed")
