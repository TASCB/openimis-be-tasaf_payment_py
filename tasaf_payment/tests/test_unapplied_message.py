from django.test import SimpleTestCase

from tasaf_payment.muse import message as mm
from tasaf_payment.muse.inbound import payment_status_key
from tasaf_payment.tests.test_muse_message import IDS, NOW, SETTINGS, fields, payee
from tasaf_payment.unapplied import MAX_ID, new_id

UNAPPLIED_SETTINGS = {**SETTINGS, 'unappliedSubBudgetClass': 355}


class UnappliedMessageTest(SimpleTestCase):
    def test_unapplied_resend_carries_type_class_and_original(self):
        body = mm.assemble(IDS, UNAPPLIED_SETTINGS, [payee(endtoEndId='PA16E5029R2', orgEndToEndId='PA16E50291')],
                           NOW, mm.UNAPPLIED_TYPE)
        self.assertEqual(body['message']['messageHeader']['paymentType'], 'UNAPPLIED')
        self.assertEqual(body['message']['paymentSummary']['subBudgetClass'], 355)
        self.assertEqual(mm.check(body), [])

    def test_ordinary_payment_keeps_the_normal_class(self):
        body = mm.assemble(IDS, UNAPPLIED_SETTINGS, [payee()], NOW)
        self.assertEqual(body['message']['messageHeader']['paymentType'], 'PAYMENT')
        self.assertEqual(body['message']['paymentSummary']['subBudgetClass'], 202)

    def test_unapplied_resend_needs_the_original_and_a_new_id(self):
        missing = mm.assemble(IDS, UNAPPLIED_SETTINGS, [payee()], NOW, mm.UNAPPLIED_TYPE)
        self.assertIn(('payList', 'orgEndToEndId', 'required'), fields(mm.check(missing)))
        same = mm.assemble(IDS, UNAPPLIED_SETTINGS, [payee(orgEndToEndId='PA16E50291')], NOW, mm.UNAPPLIED_TYPE)
        self.assertIn(('payList', 'endtoEndId', 'not_new'), fields(mm.check(same)))

    def test_unapplied_resend_without_its_class_is_refused(self):
        body = mm.assemble(IDS, SETTINGS, [payee(endtoEndId='PA16E5029R2', orgEndToEndId='PA16E50291')],
                           NOW, mm.UNAPPLIED_TYPE)
        self.assertIn(('paymentSummary', 'subBudgetClass', 'no_unapplied_class'), fields(mm.check(body)))

    def test_new_id_fits_muse_limit(self):
        self.assertEqual(new_id('1FJ8SKD9', 2), '1FJ8SKD9R2')
        long_id = new_id('XP2311451049128', 2)
        self.assertEqual(len(long_id), MAX_ID)
        self.assertTrue(long_id.endswith('R2'))

    def test_unapplied_mno_is_unapplied(self):
        self.assertEqual(payment_status_key('UNAPPLIED/MNO'), 'UNAPPLIED')
        self.assertEqual(payment_status_key(' settled '), 'SETTLED')
