import logging

from django.db import transaction
from django.dispatch import Signal

logger = logging.getLogger(__name__)

# kwargs: paylist, event ('batch_status' | 'unapplied' | 'closed'), status, description
muse_event = Signal()

# kwargs: account, status (VerificationStatus), failure_reason. Only for accounts sent after a change.
verification_event = Signal()


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
