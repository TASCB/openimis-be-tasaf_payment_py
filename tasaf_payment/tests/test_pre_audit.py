from unittest import mock

from django.test import TestCase

from tasaf_payment.models import PaymentAccount, PreAuditStatus
from tasaf_payment.services import PreAuditService


class RunPreAuditTest(TestCase):
    def setUp(self):
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        self.account = PaymentAccount.objects.filter(is_deleted=False, group_beneficiary__isnull=False).first()
        if self.user is None or self.account is None:
            self.skipTest('needs the Admin user and a payment account with a household')
        PaymentAccount.objects.filter(id=self.account.id).update(
            pre_audit_status=PreAuditStatus.PASSED, json_ext={})

    def run_with(self, reasons):
        with mock.patch.object(PreAuditService, '_check_account', return_value=reasons):
            return PreAuditService(self.user).run_pre_audit([self.account.id])

    def test_unchanged_account_does_not_fail_the_run(self):
        self.assertEqual(self.run_with([]), {'success': True, 'passed': 1, 'failed': 0, 'error': None})

    def test_failure_then_pass_records_and_clears_reasons(self):
        self.assertTrue(self.run_with(['NOT_PRIMARY'])['success'])
        account = PaymentAccount.objects.get(id=self.account.id)
        self.assertEqual((account.pre_audit_status, account.json_ext['pre_audit_failures']),
                         (PreAuditStatus.FAILED, ['NOT_PRIMARY']))
        self.assertTrue(self.run_with(['NOT_PRIMARY'])['success'])
        self.assertTrue(self.run_with([])['success'])
        account = PaymentAccount.objects.get(id=self.account.id)
        self.assertEqual(account.pre_audit_status, PreAuditStatus.PASSED)
        self.assertNotIn('pre_audit_failures', account.json_ext)
