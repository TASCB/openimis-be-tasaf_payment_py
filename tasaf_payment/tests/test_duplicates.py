from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment import duplicates as d

REPORT = {
    'households': 3, 'payments': 4, 'counts': {}, 'extra_payments': {}, 'extra_amount': 14000,
    'groups': [
        {'kind': d.SAME_HOUSEHOLD, 'key': 'HH-1', 'households': 1, 'payments': 2, 'amount': 28000,
         'rows': [{'hhid': 'HH-1', 'head': 'Asha Juma', 'account_number': '255744000001'}] * 2},
        {'kind': d.SAME_ACCOUNT, 'key': '255744000002', 'households': 2, 'payments': 2, 'amount': 28000,
         'rows': [{'hhid': 'HH-2', 'head': 'Ali Musa', 'account_number': '255744000002'},
                  {'hhid': 'HH-3', 'head': 'Neema Ali', 'account_number': '255744000002'}]},
    ],
}


class DuplicatesTest(SimpleTestCase):
    def test_account_numbers_are_compared_normalised(self):
        self.assertEqual(d.account_key('MOBILE', '0744 000 001'), d.account_key('MOBILE', '255744000001'))
        self.assertEqual(d.account_key('BANK', ' 01j 1234 '), '01J1234')

    def test_page_filters_by_kind_and_searches_households(self):
        with mock.patch.object(d, 'window_duplicates', return_value=REPORT):
            by_kind = d.duplicates_page('cycle', kind=d.SAME_ACCOUNT)
            by_name = d.duplicates_page('cycle', search='neema')
        self.assertEqual((by_kind['total_groups'], by_kind['groups'][0]['key']), (1, '255744000002'))
        self.assertEqual([g['kind'] for g in by_name['groups']], [d.SAME_ACCOUNT])
