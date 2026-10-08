import random
import re

from django.test import SimpleTestCase

from tasaf_payment.payee_code import PayeeCodeError, decode_payee_code, encode_hhid

VECTORS = [
    ('P3-020410104-82239997', 'TSFBN09LZ8JU9gj'),
    ('P3-000000000-00000000', 'TSFBN0000000000'),
    ('P3-000000000-00000001', 'TSFBN0000000001'),
    ('P3-123456789-12345678', 'TSFBN0uXgbEGOoI'),
    ('P3-999999999-99999999', 'TSFBN7O044qYiZb'),
]
MUSE_PATTERN = re.compile(r'^[0-9a-zA-Z]{1,15}$')


class PayeeCodeTest(SimpleTestCase):
    def test_vectors_both_directions(self):
        for hhid, code in VECTORS:
            self.assertEqual(encode_hhid(hhid), code)
            self.assertEqual(decode_payee_code(code), hhid)

    def test_codes_fit_muse_and_are_15_characters(self):
        for hhid, _ in VECTORS:
            code = encode_hhid(hhid)
            self.assertRegex(code, MUSE_PATTERN)
            self.assertEqual(len(code), 15)

    def test_round_trip_random_hhids(self):
        rng = random.Random(20261006)
        for _ in range(10000):
            hhid = f"P3-{rng.randrange(10 ** 9):09d}-{rng.randrange(10 ** 8):08d}"
            code = encode_hhid(hhid)
            self.assertRegex(code, MUSE_PATTERN)
            self.assertEqual(len(code), 15)
            self.assertEqual(decode_payee_code(code), hhid)

    def test_case_matters(self):
        self.assertEqual(decode_payee_code('TSFBN09LZ8JU9GJ'), 'P3-020410104-82238359')

    def test_invalid_hhids_rejected(self):
        for bad in ('P2-020410104-82239997', 'p3-020410104-82239997', 'P3-02041010-82239997',
                    'P3-020410104-8223999', 'P3-020410104-82239997-R', 'P3020410104822399',
                    ' P3-020410104-82239997', 'SEEDG0000001', '', None, 3020410104):
            with self.assertRaises(PayeeCodeError, msg=repr(bad)):
                encode_hhid(bad)

    def test_invalid_payee_codes_rejected(self):
        for bad in ('XSFBN09LZ8JU9gj', 'tsfbn09LZ8JU9gj', 'TSFBN09LZ8JU9g', 'TSFBN09LZ8JU9gjk',
                    'TSFBN09LZ8JU9g-', 'TSFBN09LZ8 U9gj', 'TSFBNzzzzzzzzzz', 'TSFBN7O044qYiZc',
                    '', None):
            with self.assertRaises(PayeeCodeError, msg=repr(bad)):
                decode_payee_code(bad)


class PayeeCodeWiringTest(SimpleTestCase):
    def test_message_payee_code_is_encoded_never_raw(self):
        from types import SimpleNamespace
        from tasaf_payment import muse_message as mm
        self.assertEqual(mm.payee_code(SimpleNamespace(code='P3-020410104-82239997')), 'TSFBN09LZ8JU9gj')
        for code in ('P3-040403202-25694267-R', 'SEEDG0000001', None):
            self.assertEqual(mm.payee_code(SimpleNamespace(code=code)), '')
        self.assertEqual(mm.payee_code(None), '')

    def test_check_names_the_missing_hhid_and_shows_hhid_next_to_code(self):
        from tasaf_payment import muse_message as mm
        from tasaf_payment.tests.test_muse_message import IDS, NOW, SETTINGS, payee
        body = mm.assemble(IDS, SETTINGS, [payee(payeeCode=''), payee(payeeCode='TSFBN09LZ8JU9gj',
                                                                        payeeBankBic='BAD')], NOW)
        problems = {p['field']: p for p in mm.check(body)}
        self.assertIn('P3 HHID', problems['payeeCode']['message'])
        self.assertEqual(problems['payeeBankBic']['hhid'], 'P3-020410104-82239997')


class InboundPayeeCodeTest(SimpleTestCase):
    def _item(self, hhid):
        from types import SimpleNamespace
        group = SimpleNamespace(code=hhid)
        return SimpleNamespace(benefit_consumption_id='b1',
                               payment_account=SimpleNamespace(group_beneficiary=SimpleNamespace(group=group)))

    def _check(self, details, item, exists=True):
        from unittest import mock
        from tasaf_payment import muse_inbound as mi
        with mock.patch('individual.models.Group.objects') as objects:
            objects.filter.return_value.exists.return_value = exists
            return mi._payee_problem(details, item)

    def test_absent_payee_code_is_ignored(self):
        self.assertIsNone(self._check({'endtoEndId': 'X'}, self._item('P3-020410104-82239997')))

    def test_matching_payee_code_passes(self):
        self.assertIsNone(self._check({'payeeCode': 'TSFBN09LZ8JU9gj'}, self._item('P3-020410104-82239997')))

    def test_uppercased_code_is_a_different_household_and_refused(self):
        reply = self._check({'payeeCode': 'TSFBN09LZ8JU9GJ'}, self._item('P3-020410104-82239997'))
        self.assertEqual(reply.http, 409)

    def test_bad_code_refused(self):
        self.assertEqual(self._check({'payeeCode': 'TSFBNzzzzzzzzzz'}, self._item('P3-1')).http, 400)
        self.assertEqual(self._check({'payeeCode': 'P3-020410104-82239997'}, self._item('P3-1')).http, 400)

    def test_unknown_household_refused(self):
        reply = self._check({'payeeCode': 'TSFBN09LZ8JU9gj'}, self._item('P3-020410104-82239997'), exists=False)
        self.assertEqual(reply.http, 404)
