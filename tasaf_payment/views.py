"""
tasaf_payment.views
====================
REST endpoints for receiving MUSE push results via GovESB.

Each inbound request is run through GovESB signature verification
(:func:`coremis_app_integration.govesb_inbound.verify_inbound`) before any
business handling. In production (when ``settings.ESB`` is configured) a valid
ECDSA-signed ``{data, signature}`` envelope is **required**, verified against
the ESB public key; otherwise the request is rejected with HTTP 401. When ESB
is not configured these endpoints stay open for development/testing and
admin-override with bare JSON payloads — see ``govesb_inbound`` for the policy.
"""

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
    """
    Verify an inbound GovESB push and return ``(business_payload, error)``.

    ``error`` non-``None`` ⇒ the caller must reject with HTTP 401. Fail-closed:
    if signature verification is required (ESB configured) but the verifier
    cannot be loaded, the request is rejected rather than trusted.
    """
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
    """
    POST /api/tasaf_payment/muse/return_feedback/

    Receives return / unapplied feedback pushed by MUSE over GovESB. In
    production the request body is a signed ``{data, signature}`` envelope; the
    verified business payload (``esbBody``) has the shape:
    {
        "paylist_item_uuid":  "<uuid>",
        "feedback_type":      "UNAPPLIED",
        "reason_code":        "...",
        "reason_description": "...",
        "muse_reference":     "..."
    }
    """

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
    """
    POST /api/tasaf_payment/muse/settlement/

    Receives **successful** payment confirmations pushed by the gateway over GovESB —
    the counterpart to :class:`MuseReturnFeedbackView`, which only carries failures.
    Without this endpoint nothing sets ``PaylistItem.status = PROCESSED``, so a paid
    item is indistinguishable from one that was merely sent.

    Single item::

        {"paylist_item_uuid": "<uuid>", "muse_reference": "...", "settled_at": "ISO8601"}

    Batch — the shape a settlement file takes::

        {"items": [{"paylist_item_uuid": "...", ...}, ...]}

    A batch reports partial success rather than failing wholesale: unknown or
    already-returned items are listed in ``rejected`` and the rest still settle.
    """

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
    """
    POST /api/tasaf_payment/muse/message/

    Any message MUSE sends, in MUSE's own format: ``{"message": {messageHeader, messageSummary |
    messageDetails}, "digitalSignature"}`` inside the signed GovESB envelope.
    Always HTTP 200 with a signed reply: success + TASAF's ACK, or success=false + message.
    """

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

        if request.body.lstrip().startswith(b'<'):
            return self._xml(request.body.decode('utf-8', 'replace'), handle)

        from coremis_app_integration.govesb_inbound import signed_reply

        def reply(success, body):
            text = signed_reply(True, esb_body=body) if success else signed_reply(False, message=body)
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

        def reply(success, body):
            text = (signed_reply_xml(True, esb_body=gepg_message.as_xml(body)) if success
                    else signed_reply_xml(False, message=body))
            return HttpResponse(text, content_type='application/xml', status=200)

        body, _, verr = verify_inbound_xml(raw)
        if verr:
            logger.warning("[GovESB] inbound GePG XML message rejected: %s", verr)
            return reply(False, verr)
        return reply(*handle(gepg_message.load(body) or {}))
