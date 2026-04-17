from django.urls import path
from tasaf_payment.views import MuseVerificationResultView, MuseReturnFeedbackView

urlpatterns = [
    # Stub endpoints — receive MUSE push results for dev/testing.
    # TODO (GovESB): These will be supplemented/replaced by the GovESB consumer.
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
]
