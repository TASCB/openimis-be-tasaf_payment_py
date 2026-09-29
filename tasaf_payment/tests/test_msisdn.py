from django.test import SimpleTestCase

from tasaf_payment import msisdn


class MsisdnTest(SimpleTestCase):
    def test_unambiguous_forms_are_normalised(self):
        for raw in ('255712345678', '+255712345678', '+255 712 345 678', '0712345678', '0712-345-678',
                    '712345678', '(255) 712345678'):
            self.assertEqual(msisdn.normalise(raw), '255712345678', raw)
        self.assertEqual(msisdn.normalise('0612345678'), '255612345678')

    def test_a_digit_too_many_is_not_guessed(self):
        self.assertEqual(msisdn.normalise('2557982429787'), '2557982429787')
        self.assertFalse(msisdn.is_valid(msisdn.normalise('2557982429787')))

    def test_validity(self):
        self.assertTrue(msisdn.is_valid('255712345678'))
        self.assertTrue(msisdn.is_valid('255612345678'))
        for bad in ('255812345678', '25571234567', '2557123456789', '0712345678', '', None):
            self.assertFalse(msisdn.is_valid(bad), bad)
