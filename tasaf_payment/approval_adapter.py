"""Mirror terminal approval-engine outcomes onto TASAF paylists."""
import logging
from datetime import datetime, timezone as dtz

logger = logging.getLogger(__name__)

DOMAIN = 'tasaf_payment.Paylist'


class PaylistApprovalAdapter:

    @staticmethod
    def on_finalized(**kwargs):
        try:
            result = kwargs.get('result') or {}
            data = result.get('data') or {}
            if data.get('domain') != DOMAIN:
                return

            from approval.models import ApprovalRequest
            from tasaf_payment.models import Paylist, PaylistStatus
            from tasaf_payment.services import _inbound_audit_user

            appr = ApprovalRequest.objects.filter(id=data.get('id')).first()
            if not appr:
                return
            paylist = Paylist.objects.filter(id=appr.object_id, is_deleted=False).first()
            if not paylist:
                logger.warning("paylist adapter: Paylist %s not found", appr.object_id)
                return

            # The engine passes its service instance; its user signed the final decision.
            user = getattr(kwargs.get('cls_'), 'user', None) or _inbound_audit_user(paylist)
            decision = data.get('decision')
            if decision == 'APPROVED':
                paylist.status = PaylistStatus.APPROVED
                paylist.approved_at = datetime.now(tz=dtz.utc)
                paylist.save(user=user)
                logger.info("paylist %s -> APPROVED via approval engine", paylist.id)
            elif decision in ('REJECTED', 'CANCELLED'):
                paylist.status = PaylistStatus.REJECTED_AT_APPROVAL
                paylist.save(user=user)
                logger.info("paylist %s -> REJECTED_AT_APPROVAL (%s) via approval engine", paylist.id, decision)
        except Exception as exc:
            logger.error("tasaf_payment paylist approval adapter failed", exc_info=exc)
            return [str(exc)]
