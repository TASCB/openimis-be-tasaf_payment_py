import logging

from django.db import transaction
from django.dispatch import Signal

logger = logging.getLogger(__name__)

# kwargs: paylist, event ('batch_status' | 'unapplied' | 'closed'), status, description
muse_event = Signal()

# kwargs: account, status (VerificationStatus), failure_reason. Only for accounts sent after a change.
verification_event = Signal()

# kwargs: accounts (the PaymentAccounts a GePG send held back), user
accounts_withdrawn = Signal()

# kwargs: request (GepgVerificationRequest), counts {decision: n}
gepg_answered = Signal()

# kwargs: request (GepgVerificationRequest), minutes (how long it has waited)
gepg_unanswered = Signal()


def emit_muse_event(paylist, event, **details):
    if not muse_event.has_listeners():
        return

    def send():
        for receiver, result in muse_event.send_robust(sender=None, paylist=paylist, event=event, **details):
            if isinstance(result, Exception):
                logger.warning("muse_event receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)


def emit_verification_event(account, status, failure_reason=None):
    if not verification_event.has_listeners():
        return

    def send():
        for receiver, result in verification_event.send_robust(
                sender=None, account=account, status=status, failure_reason=failure_reason):
            if isinstance(result, Exception):
                logger.warning("verification_event receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)


def emit_accounts_withdrawn(accounts, user):
    if not accounts or not accounts_withdrawn.has_listeners():
        return

    def send():
        for receiver, result in accounts_withdrawn.send_robust(sender=None, accounts=accounts, user=user):
            if isinstance(result, Exception):
                logger.warning("accounts_withdrawn receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)


def emit_gepg_answered(request, counts):
    if not gepg_answered.has_listeners():
        return

    def send():
        for receiver, result in gepg_answered.send_robust(sender=None, request=request, counts=counts):
            if isinstance(result, Exception):
                logger.warning("gepg_answered receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)


def emit_gepg_unanswered(request, minutes):
    if not gepg_unanswered.has_listeners():
        return

    def send():
        for receiver, result in gepg_unanswered.send_robust(sender=None, request=request, minutes=minutes):
            if isinstance(result, Exception):
                logger.warning("gepg_unanswered receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)


account_check_done = Signal()


def emit_account_check_done(job):
    if not account_check_done.has_listeners():
        return

    def send():
        for receiver, result in account_check_done.send_robust(sender=None, job=job):
            if isinstance(result, Exception):
                logger.warning("account_check_done receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)
