from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment.apps import TasafPaymentConfig
from tasaf_payment.models import VerificationStatus
from tasaf_payment.services import FIRST_PAYMENT_CHECK, PreAuditService


def account(**kw):
    values = {
        'id': 1, 'verification_status': VerificationStatus.VERIFIED, 'is_primary': True,
        'group_beneficiary': SimpleNamespace(status='ACTIVE'), 'fsp_type': 'MOBILE',
        'account_number': '255754000111', 'fsp_name': 'Vodacom M-Pesa',
        'json_ext': {FIRST_PAYMENT_CHECK: {'account_number': '255754000111', 'fsp_name': 'Vodacom M-Pesa'}},
    }
    values.update(kw)
    return SimpleNamespace(**values)


@mock.patch.object(TasafPaymentConfig, 'require_first_payment_check', True)
class PreAuditRulesTest(SimpleTestCase):
    def check(self, acc, paid_or_in_flight=frozenset()):
        return PreAuditService._check_account(acc, set(paid_or_in_flight))

    def test_clean_account_passes(self):
        self.assertEqual(self.check(account()), [])

    def test_each_rule_names_its_reason(self):
        cases = [
            (account(verification_status=VerificationStatus.MANUAL), 'NOT_VERIFIED'),
            (account(is_primary=False), 'NOT_PRIMARY'),
            (account(group_beneficiary=None), 'NO_BENEFICIARY'),
            (account(group_beneficiary=SimpleNamespace(status='SUSPENDED')), 'BENEFICIARY_INACTIVE'),
            (account(account_number='0754', json_ext={}), 'INVALID_MOBILE_NUMBER'),
        ]
        for acc, reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(reason, self.check(acc))

    def test_bank_numbers_are_not_checked_as_mobile(self):
        self.assertNotIn('INVALID_MOBILE_NUMBER', self.check(account(fsp_type='BANK', account_number='0150-ABC')))

    def test_first_payment_check_due_until_checked_for_this_number(self):
        self.assertIn('FIRST_PAYMENT_CHECK_DUE', self.check(account(json_ext={})))
        changed = account(account_number='255754999999')
        self.assertIn('FIRST_PAYMENT_CHECK_DUE', self.check(changed))

    def test_first_payment_check_not_due_once_paid_or_in_flight(self):
        self.assertNotIn('FIRST_PAYMENT_CHECK_DUE', self.check(account(json_ext={}), {1}))

    def test_first_payment_check_can_be_switched_off(self):
        with mock.patch.object(TasafPaymentConfig, 'require_first_payment_check', False):
            self.assertNotIn('FIRST_PAYMENT_CHECK_DUE', self.check(account(json_ext={})))
