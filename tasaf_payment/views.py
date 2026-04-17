"""
tasaf_payment.views
====================
Stub REST endpoints for receiving MUSE push results via GovESB.

These endpoints exist for development and testing. When the GovESB adaptor
is available, the inbound handling will be triggered by the GovESB consumer
in coremis_app_integration instead — these endpoints become optional
(can be kept for manual testing / admin override).

TODO (GovESB): Wire MuseVerificationInboundService.handle_result() and
ReturnFeedbackService.handle_feedback() to the GovESB consumer.
"""

import logging

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


@method_decorator(csrf_exempt, name='dispatch')
class MuseVerificationResultView(View):
    """
    POST /api/tasaf_payment/muse/verification_result/

    Receives a verification result payload pushed by MUSE (via GovESB stub).

    Expected payload:
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

        if not payload.get('account_uuid'):
            return JsonResponse({'success': False, 'error': 'account_uuid is required'}, status=400)
        if not payload.get('result'):
            return JsonResponse({'success': False, 'error': 'result is required'}, status=400)

        logger.info(
            "[GovESB STUB] Inbound verification result: account=%s result=%s ref=%s",
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

    Receives return / unapplied feedback pushed by MUSE (via GovESB stub).

    Expected payload:
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

        if not payload.get('paylist_item_uuid'):
            return JsonResponse({'success': False, 'error': 'paylist_item_uuid is required'}, status=400)
        if not payload.get('feedback_type'):
            return JsonResponse({'success': False, 'error': 'feedback_type is required'}, status=400)

        logger.info(
            "[GovESB STUB] Inbound return feedback: item=%s type=%s code=%s",
            payload.get('paylist_item_uuid'),
            payload.get('feedback_type'),
            payload.get('reason_code'),
        )

        service = ReturnFeedbackService()
        result = service.handle_feedback(payload)

        status_code = 200 if result.get('success') else 400
        return JsonResponse(result, status=status_code)
