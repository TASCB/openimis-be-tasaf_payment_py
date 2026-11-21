import json
import logging
import re

logger = logging.getLogger(__name__)

MSG_ID = re.compile(r'"msgId"\s*:\s*"([^"]{1,50})"')
MAX_TEXT = 200_000


def record(raw, accepted, reply):
    from tasaf_payment.models import TipsMessageLog
    try:
        text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw or '')
        try:
            from tasaf_payment.tips.message import without_identity
            text = json.dumps(without_identity(json.loads(text)), ensure_ascii=False)
        except ValueError:
            pass
        found = MSG_ID.search(text)
        TipsMessageLog.objects.create(msg_id=found.group(1) if found else '', body=text[:MAX_TEXT],
                                      accepted=bool(accepted), reply=str(reply or '')[:MAX_TEXT])
    except Exception:  # noqa: BLE001
        logger.warning("TIPS inbound message could not be logged", exc_info=True)
