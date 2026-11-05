from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment.models import PaylistStatus
from tasaf_payment.services import PaylistService


class PaylistApproveGuardTest(SimpleTestCase):
    def approve(self, request):
        paylist = SimpleNamespace(status=PaylistStatus.PENDING_APPROVAL, save=mock.Mock())
        service = PaylistService(SimpleNamespace(username='approver'))
        with mock.patch('tasaf_payment.services.Paylist.objects.get', return_value=paylist), \
                mock.patch('tasaf_payment.services._wrong_type_refusal', return_value=None), \
                mock.patch.object(PaylistService, '_engine_request_for', return_value=request):
            return service.approve('00000000-0000-0000-0000-000000000001'), paylist

    def test_paylist_without_an_approval_request_is_refused(self):
        result, paylist = self.approve(None)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'tasaf_payment.error.no_approval_request')
        self.assertEqual(paylist.status, PaylistStatus.PENDING_APPROVAL)
        paylist.save.assert_not_called()
