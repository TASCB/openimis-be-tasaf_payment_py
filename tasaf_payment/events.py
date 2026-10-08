import logging

from django.db import transaction
from django.dispatch import Signal

logger = logging.getLogger(__name__)

# kwargs: paylist, event ('batch_status' | 'unapplied' | 'closed'), status, description
muse_event = Signal()


def emit_muse_event(paylist, event, **details):
    if not muse_event.has_listeners():
        return

    def send():
        for receiver, result in muse_event.send_robust(sender=None, paylist=paylist, event=event, **details):
            if isinstance(result, Exception):
                logger.warning("muse_event receiver %s failed: %s", receiver, result)
    transaction.on_commit(send)
