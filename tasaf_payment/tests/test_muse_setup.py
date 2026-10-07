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


class DescribeChangeTest(SimpleTestCase):
    def test_settings_change_reads_as_before_and_after(self):
        summary = ms.describe_change(
            'SETTINGS', '', {'sub_budget_class': None, 'institution_code': 'TEST', 'is_stp': False},
            {'sub_budget_class': 202, 'institution_code': 'TEST', 'is_stp': True},
            reason='  Value from MUSE  ')
        self.assertEqual(summary['title'], 'MUSE settings')
        self.assertEqual(summary['reason'], 'Value from MUSE')
        self.assertNotIn('server', summary)
        self.assertEqual(summary['changes'][:2], [
            {'label': 'Sub-budget class', 'before': None, 'after': '202', 'changed': True},
            {'label': 'Straight-through processing', 'before': 'No', 'after': 'Yes', 'changed': True}])
        self.assertEqual(summary['changes'][2], {'label': 'Institution code', 'before': 'TEST', 'after': 'TEST',
                                                 'changed': False})

    def test_fsp_change_names_the_provider_and_reads_gl_lines(self):
        summary = ms.describe_change('FSP_PROFILE', 'MPESA', {}, {'bank_name': 'VODACOM', 'fsp_type': 'MOBILE',
                                                                   'bic': 'VODATZTX'})
        self.assertEqual(summary['title'], 'FSP routing: VODACOM (MPESA)')
        self.assertIsNone(summary['reason'])
        self.assertEqual({c['label']: c['after'] for c in summary['changes']},
                         {'Bank name': 'VODACOM', 'Channel': 'MOBILE', 'BIC': 'VODATZTX'})
        gl = ms.describe_change('SETTINGS', '', {}, {'gl_accounts': [
            {'glaccount': '22010101', 'glaccountDesc': 'Cash transfers', 'grantName': ''}]})
        self.assertEqual(gl['changes'][0]['after'], '22010101 — Cash transfers')

    def test_empty_to_empty_is_not_a_change(self):
        summary = ms.describe_change('SETTINGS', '', {'gl_accounts': None}, {'gl_accounts': []})
        self.assertFalse(summary['changes'][0]['changed'])


class PaylistSummaryFormatTest(SimpleTestCase):
    def test_amounts_read_as_shillings(self):
        from decimal import Decimal
        from tasaf_payment.services import _tzs
        self.assertEqual(_tzs(Decimal('3010000.00')), 'TZS 3,010,000')
        self.assertEqual(_tzs(Decimal('1250.50')), 'TZS 1,250.50')
        self.assertEqual(_tzs(None), 'TZS 0')
