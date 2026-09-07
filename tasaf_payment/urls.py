from django.urls import path
from tasaf_payment.views import (
    MuseVerificationResultView,
    MuseReturnFeedbackView,
    MuseSettlementView,
)

urlpatterns = [
    path(
        'muse/verification_result/',
        MuseVerificationResultView.as_view(),
        name='muse-verification-result',
    ),
    path(
        'muse/return_feedback/',
        MuseReturnFeedbackView.as_view(),
        name='muse-return-feedback',
    ),
    # Successful payment confirmations — the counterpart to return_feedback.
    path(
        'muse/settlement/',
        MuseSettlementView.as_view(),
        name='muse-settlement',
    ),
]
