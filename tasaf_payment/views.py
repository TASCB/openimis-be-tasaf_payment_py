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
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.views import View

from tasaf_payment.services import MuseVerificationInboundService, ReturnFeedbackService

logger = logging.getLogger(__name__)


def _parse_json_body(request):
    import json
    try:
        return json.loads(request.body), None
    except (ValueError, TypeError) as exc:
        return None, str(exc)


def _verify_inbound(payload):
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

    business, verified, error = verify_inbound(payload)
    if error:
        return None, error
    if verified:
        logger.info("[GovESB] inbound signature verified")
    return business, None


@method_decorator(csrf_exempt, name='dispatch')
class MuseVerificationResultView(View):
    """
    POST /api/tasaf_payment/muse/verification_result/

    Receives a verification result pushed by MUSE over GovESB. In production the
    request body is a signed ``{data, signature}`` envelope; the verified
    business payload (``esbBody``) has the shape:
    {
        "account_uuid":       "<uuid>",
        "muse_reference":     "MUSE-REF-123",
        "verification_type":  "FSP_ACCOUNT",
        "result":             "PASSED" | "FAILED" | "MANUAL",
        "failure_reason":     "..." | null,
        "raw_response":       {}
    }
    """

    def post(self, request, *args, **kwargs):
        payload, error = _parse_json_body(request)
        if error:
            return JsonResponse({'success': False, 'error': f'Invalid JSON: {error}'}, status=400)

        payload, verr = _verify_inbound(payload)
        if verr:
            logger.warning("[GovESB] inbound verification result rejected: %s", verr)
            return JsonResponse({'success': False, 'error': verr}, status=401)

        if not payload.get('account_uuid'):
            return JsonResponse({'success': False, 'error': 'account_uuid is required'}, status=400)
        if not payload.get('result'):
            return JsonResponse({'success': False, 'error': 'result is required'}, status=400)

        logger.info(
            "Inbound verification result: account=%s result=%s ref=%s",
            payload.get('account_uuid'),
            payload.get('result'),
            payload.get('muse_reference'),
        )

        service = MuseVerificationInboundService()
        result = service.handle_result(payload)

        status_code = 200 if result.get('success') else 400
        return JsonResponse(result, status=status_code)


@method_decorator(csrf_exempt, name='dispatch')
class MuseReturnFeedbackView(View):
    """
    POST /api/tasaf_payment/muse/return_feedback/

    Receives return / unapplied feedback pushed by MUSE over GovESB. In
    production the request body is a signed ``{data, signature}`` envelope; the
    verified business payload (``esbBody``) has the shape:
    {
        "paylist_item_uuid":  "<uuid>",
        "feedback_type":      "UNAPPLIED" | "RETURNED" | "PARTIAL",
        "reason_code":        "...",
        "reason_description": "...",
        "muse_reference":     "..."
    }
    """

    def post(self, request, *args, **kwargs):
        payload, error = _parse_json_body(request)
        if error:
            return JsonResponse({'success': False, 'error': f'Invalid JSON: {error}'}, status=400)

        payload, verr = _verify_inbound(payload)
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
