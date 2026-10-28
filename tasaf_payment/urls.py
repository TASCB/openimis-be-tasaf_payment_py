from django.urls import path
from tasaf_payment.views import (
    MuseReturnFeedbackView,
    MuseSettlementView,
    MuseMessageView,
    GepgMessageView,
    account_check_upload,
)

urlpatterns = [
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
    # Every message MUSE sends: ACK / RESPONSE (batch) and PAYMENT_STATUS (per payment).
    path(
        'muse/message/',
        MuseMessageView.as_view(),
        name='muse-message',
    ),
    path('account_check/upload/', account_check_upload, name='account-check-upload'),
    path(
        'gepg/message/',
        GepgMessageView.as_view(),
        name='gepg-message',
    ),
]
