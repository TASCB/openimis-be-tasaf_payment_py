from django.test import TestCase


class PaymentAccountServiceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_module_loaded(self):
        from tasaf_payment.apps import TasafPaymentConfig
        self.assertEqual(TasafPaymentConfig.name, 'tasaf_payment')

    def test_models_importable(self):
        from tasaf_payment.models import PaymentAccount, VerificationRecord, PaymentAccountMutation
        self.assertIsNotNone(PaymentAccount)
        self.assertIsNotNone(VerificationRecord)
        self.assertIsNotNone(PaymentAccountMutation)
