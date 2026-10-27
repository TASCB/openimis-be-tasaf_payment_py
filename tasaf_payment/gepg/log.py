import logging
import re

logger = logging.getLogger(__name__)

ACC_VERIF_ID = (re.compile(r'<AccVerifId>\s*([^<\s]{1,50})'), re.compile(r'"AccVerifId"\s*:\s*"([^"]{1,50})"'))
MAX_TEXT = 200_000


def record(raw, data_format, accepted, reply):
    """Keep the message and our answer; a failure to log never changes the answer."""
    from tasaf_payment.models import GepgMessageLog
    try:
        text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw or '')
        found = next((m.group(1) for p in ACC_VERIF_ID for m in [p.search(text)] if m), '')
        GepgMessageLog.objects.create(acc_verif_id=found, data_format=data_format, body=text[:MAX_TEXT],
                                      accepted=bool(accepted), reply=str(reply or '')[:MAX_TEXT])
    except Exception:  # noqa: BLE001
        logger.warning("GePG inbound message could not be logged", exc_info=True)
