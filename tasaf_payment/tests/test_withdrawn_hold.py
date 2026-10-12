from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment import withdrawn_hold as wh


def item(withdrawn_at=None, updated=datetime(2026, 10, 9, 12, 0)):
    return SimpleNamespace(json_ext={'withdrawn_at': withdrawn_at} if withdrawn_at else {}, date_updated=updated)


class DeadlineTest(SimpleTestCase):
    def setUp(self):
        for name, value in (('withdrawn_hold_grace_days', 0), ('withdrawn_hold_days_without_cycle', 30)):
            patcher = mock.patch(f'tasaf_payment.apps.TasafPaymentConfig.{name}', value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cycle_end_is_the_cut_off(self):
        payroll = SimpleNamespace(payment_cycle=SimpleNamespace(end_date=date(2026, 10, 31)))
        self.assertEqual(wh.deadline(payroll, item()), date(2026, 10, 31))

    def test_grace_days_extend_it(self):
        payroll = SimpleNamespace(payment_cycle=SimpleNamespace(end_date=date(2026, 10, 31)))
        with mock.patch('tasaf_payment.apps.TasafPaymentConfig.withdrawn_hold_grace_days', 7):
            self.assertEqual(wh.deadline(payroll, item()), date(2026, 11, 7))

    def test_without_a_cycle_days_count_from_the_withdrawal(self):
        payroll = SimpleNamespace(payment_cycle=None)
        self.assertEqual(wh.deadline(payroll, item('2026-10-09T22:30:00+00:00')), date(2026, 11, 9))
        self.assertEqual(wh.deadline(payroll, item()), date(2026, 11, 8))
