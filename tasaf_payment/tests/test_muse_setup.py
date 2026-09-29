from django.test import SimpleTestCase

from tasaf_payment import muse_setup as ms


class MuseSetupValidationTest(SimpleTestCase):
    def test_values_are_trimmed_and_currency_is_not_an_input(self):
        values = ms.normalise_settings({'currency_code': 'USD', 'institution_code': ' 000M0048 '})
        self.assertNotIn('currency_code', values)
        self.assertEqual(values['institution_code'], '000M0048')

    def test_budget_classes_must_be_whole_numbers(self):
        with self.assertRaises(ms.SetupError):
            ms.normalise_settings({'sub_budget_class': 'abc'})
        self.assertIsNone(ms.normalise_settings({'sub_budget_class': ''})['sub_budget_class'])
        self.assertEqual(ms.normalise_settings({'sub_budget_class': '202'})['sub_budget_class'], 202)

    def test_gl_line_needs_an_account_code(self):
        with self.assertRaises(ms.SetupError):
            ms.normalise_settings({'gl_accounts': [{'glaccountDesc': 'x'}]})
        lines = ms.normalise_settings({'gl_accounts': [{'glaccount': ' 052|0048 ', 'grantName': 'TZAM'}]})['gl_accounts']
        self.assertEqual(lines, [{'glaccount': '052|0048', 'glaccountDesc': '', 'grantName': 'TZAM'}])

    def test_profile_rules(self):
        code, values = ms.normalise_profile('crdb', 'CRDB Bank', 'bank', 'corutztz')
        self.assertEqual((code, values['fsp_type'], values['bic']), ('CRDB', 'BANK', 'CORUTZTZ'))
        for args in (('CRDB', '', 'BANK', 'CORUTZTZ'), ('CRDB', 'x', 'CASH', 'CORUTZTZ'),
                     ('CRDB', 'x', 'BANK', 'CORUKEKX')):
            with self.assertRaises(ms.SetupError):
                ms.normalise_profile(*args)
