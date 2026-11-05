"""GovESB inbound endpoints (MUSE, GePG), signature-checked first, and the bulk account-check upload."""

import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views import View

from tasaf_payment.services import PaymentSettlementService, ReturnFeedbackService

logger = logging.getLogger(__name__)


def _parse_json_body(request):
    import json
    try:
        return json.loads(request.body), None
    except (ValueError, TypeError) as exc:
        return None, str(exc)


def _verify_inbound(payload, raw=None):
    """Verify an inbound GovESB push and return ``(business_payload, error)``."""
    try:
        from coremis_app_integration.govesb_inbound import verify_inbound
    except ImportError:
        esb = getattr(settings, 'ESB', None) or {}
        required = bool(esb) and bool(esb.get('VERIFY_INBOUND_SIGNATURE', esb.get('ENABLED', True)))
        if required:
            return None, "GovESB inbound verification module unavailable"
        return payload, None

    business, verified, error = verify_inbound(payload, raw)
    if error:
        return None, error
    if verified:
        logger.info("[GovESB] inbound signature verified")
    return business, None


@method_decorator(csrf_exempt, name='dispatch')
class MuseReturnFeedbackView(View):
    """POST muse/return_feedback/: one returned (unapplied) payment from MUSE."""

    def post(self, request, *args, **kwargs):
        payload, error = _parse_json_body(request)
        if error:
            return JsonResponse({'success': False, 'error': f'Invalid JSON: {error}'}, status=400)

        payload, verr = _verify_inbound(payload, request.body)
        if verr:
            logger.warning("[GovESB] inbound return feedback rejected: %s", verr)
            return JsonResponse({'success': False, 'error': verr}, status=401)

        if not payload.get('paylist_item_uuid'):
            return JsonResponse({'success': False, 'error': 'paylist_item_uuid is required'}, status=400)
        if not payload.get('feedback_type'):
            return JsonResponse({'success': False, 'error': 'feedback_type is required'}, status=400)

        logger.info(
            "Inbound return feedback: item=%s type=%s code=%s",
            payload.get('paylist_item_uuid'),
            payload.get('feedback_type'),
            payload.get('reason_code'),
        )

        service = ReturnFeedbackService()
        result = service.handle_feedback(payload)

        status_code = 200 if result.get('success') else 400
        return JsonResponse(result, status=status_code)


@method_decorator(csrf_exempt, name='dispatch')
class MuseSettlementView(View):
    """POST muse/settlement/: successful payments, one item or a batch; the only path that marks items PROCESSED."""

    def post(self, request, *args, **kwargs):
        payload, error = _parse_json_body(request)
        if error:
            return JsonResponse({'success': False, 'error': f'Invalid JSON: {error}'}, status=400)

        payload, verr = _verify_inbound(payload, request.body)
        if verr:
            logger.warning("[GovESB] inbound settlement rejected: %s", verr)
            return JsonResponse({'success': False, 'error': verr}, status=401)

        service = PaymentSettlementService()

        if isinstance(payload.get('items'), list):
            logger.info("Inbound settlement batch: %d item(s)", len(payload['items']))
            result = service.handle_batch_settlement(payload)
            return JsonResponse(result, status=200 if result.get('success') else 400)

        if not payload.get('paylist_item_uuid'):
            return JsonResponse(
                {'success': False, 'error': "paylist_item_uuid is required (or 'items' for a batch)"},
                status=400,
            )

        logger.info(
            "Inbound settlement: item=%s ref=%s",
            payload.get('paylist_item_uuid'), payload.get('muse_reference'),
        )
        result = service.handle_settlement(payload)
        return JsonResponse(result, status=200 if result.get('success') else 400)


@method_decorator(csrf_exempt, name='dispatch')
class MuseMessageView(View):
    """POST muse/message/: any MUSE message in MUSE's own format; always answers 200 with a signed reply."""

    def post(self, request, *args, **kwargs):
        from coremis_app_integration.govesb_inbound import signed_reply
        from tasaf_payment.muse.inbound import handle

        def reply(success, body):
            if success:
                text = signed_reply(True, esb_body=body)
            else:
                text = signed_reply(False, message=body)
            return HttpResponse(text, content_type='application/json', status=200)

        payload, error = _parse_json_body(request)
        if error:
            return reply(False, f'Invalid JSON: {error}')

        payload, verr = _verify_inbound(payload, request.body)
        if verr:
            logger.warning("[GovESB] inbound MUSE message rejected: %s", verr)
            return reply(False, verr)

        return reply(*handle(payload))


@method_decorator(csrf_exempt, name='dispatch')
class GepgMessageView(View):
    """POST /api/tasaf_payment/gepg/message/ — GePG's gepgAccVerifSubResp over GovESB, XML or JSON;
    answered in the format it came in."""

    def post(self, request, *args, **kwargs):
        from tasaf_payment.gepg.inbound import handle

        from tasaf_payment.gepg.log import record

        if request.body.lstrip().startswith(b'<'):
            return self._xml(request.body.decode('utf-8', 'replace'), handle)

        from coremis_app_integration.govesb_inbound import signed_reply

        def reply(success, body):
            text = signed_reply(True, esb_body=body) if success else signed_reply(False, message=body)
            record(request.body, 'JSON', success, text)
            return HttpResponse(text, content_type='application/json', status=200)

        payload, error = _parse_json_body(request)
        if error:
            return reply(False, f'Invalid JSON: {error}')
        payload, verr = _verify_inbound(payload, request.body)
        if verr:
            logger.warning("[GovESB] inbound GePG message rejected: %s", verr)
            return reply(False, verr)
        return reply(*handle(payload))

    @staticmethod
    def _xml(raw, handle):
        from coremis_app_integration.govesb_inbound import signed_reply_xml, verify_inbound_xml
        from tasaf_payment.gepg import message as gepg_message
        from tasaf_payment.gepg.log import record

        def reply(success, body):
            text = (signed_reply_xml(True, esb_body=gepg_message.as_xml(body)) if success
                    else signed_reply_xml(False, message=body))
            record(raw, 'XML', success, text)
            return HttpResponse(text, content_type='application/xml', status=200)

        body, _, verr = verify_inbound_xml(raw)
        if verr:
            logger.warning("[GovESB] inbound GePG XML message rejected: %s", verr)
            return reply(False, verr)
        return reply(*handle(gepg_message.load(body) or {}))


# ── Bulk account check upload ────────────────────────────────────────────────

from rest_framework import status as _status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from core.views import check_user_rights
from tasaf_payment.apps import TasafPaymentConfig


@api_view(['POST'])
@parser_classes([MultiPartParser])
@permission_classes([check_user_rights(TasafPaymentConfig.gql_account_check_perms or ['270104'])])
def account_check_upload(request):
    from tasaf_payment.account_check import AccountCheckError, create_job, summary
    upload = request.FILES.get('file')
    if not upload:
        return Response({'success': False, 'error': 'No file'}, status=_status.HTTP_400_BAD_REQUEST)
    max_mb = int(getattr(TasafPaymentConfig, 'account_check_max_mb', 10) or 10)
    if upload.size > max_mb * 1024 * 1024:
        error = f'The file is larger than {max_mb} MB; split it and upload each part'
        return Response({'success': False, 'error': error}, status=_status.HTTP_400_BAD_REQUEST)
    try:
        job = create_job(request.user, upload.name, upload.read())
    except AccountCheckError as exc:
        return Response({'success': False, 'error': str(exc)}, status=_status.HTTP_400_BAD_REQUEST)
    return Response({'success': True, **summary(job)})
