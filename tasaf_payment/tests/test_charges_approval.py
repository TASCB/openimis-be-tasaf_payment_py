from unittest import mock

from django.test import TestCase

from tasaf_payment import charges
from tasaf_payment.apps import TasafPaymentConfig
from tasaf_payment.models import MuseChangeKind, MuseChangeRequest, MuseChangeStatus
from tasaf_payment.muse import setup

CODE, OTHER = 'TSTFSP', 'TSTOTHER'
BANDS = [{'lower_amount': 1, 'upper_amount': 9999, 'withdrawal': 500},
         {'lower_amount': 10000, 'upper_amount': 99999, 'withdrawal': 1000}]


def csv_of(rows):
    return 'EPAYMENT_CODE,LOWER_AMOUNT,UPPER_AMOUNT,WITHDRAWAL\n' + '\n'.join(','.join(map(str, r)) for r in rows)


class ChargesApprovalTest(TestCase):
    def setUp(self):
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        if self.user is None:
            self.skipTest('needs the Admin user')
        engine = mock.patch('approval.services.ApprovalService.request_approval', return_value={'success': True})
        engine.start()
        self.addCleanup(engine.stop)
        charges.apply_band_set(CODE, BANDS, self.user)
        charges.apply_band_set(OTHER, BANDS, self.user)

    def pending(self, code=CODE):
        return MuseChangeRequest.objects.filter(kind=MuseChangeKind.FSP_CHARGES, fsp_code=code,
                                                status=MuseChangeStatus.PENDING)

    def test_band_change_waits_for_approval_then_applies(self):
        new = BANDS + [{'lower_amount': 100000, 'upper_amount': 199999, 'withdrawal': 1500}]
        self.assertEqual(charges.save_band_set(self.user, CODE, new), charges.PROPOSED)
        self.assertEqual(len(charges.band_set(CODE)), 2)
        change = self.pending().get()
        self.assertEqual(len(change.current['bands']), 2)

        setup._apply(change, self.user)
        self.assertEqual(len(charges.band_set(CODE)), 3)

    def test_same_bands_open_nothing(self):
        self.assertEqual(charges.save_band_set(self.user, CODE, BANDS), charges.UNCHANGED)
        self.assertFalse(self.pending().exists())

    def test_second_change_while_one_waits_is_refused(self):
        charges.save_band_set(self.user, CODE, BANDS[:1])
        with self.assertRaises(setup.SetupError):
            charges.save_band_set(self.user, CODE, BANDS[1:])

    def test_without_approval_applies_at_once(self):
        with mock.patch.object(TasafPaymentConfig, 'charges_require_approval', False):
            self.assertEqual(charges.save_band_set(self.user, CODE, BANDS[:1]), charges.APPLIED)
        self.assertEqual(len(charges.band_set(CODE)), 1)

    def test_import_proposes_per_fsp_and_leaves_other_fsps(self):
        outcome = charges.import_csv(csv_of([(CODE, 100000, 199999, 1500)]), self.user)
        self.assertEqual(outcome, {CODE: charges.PROPOSED})
        self.assertEqual(len(self.pending().get().proposed['bands']), 3)
        self.assertFalse(self.pending(OTHER).exists())
        self.assertEqual(len(charges.band_set(CODE)), 2)

    def test_import_replace_uses_only_the_file(self):
        charges.import_csv(csv_of([(CODE, 1, 199999, 700)]), self.user, replace=True)
        self.assertEqual(len(self.pending().get().proposed['bands']), 1)

    def test_import_with_a_bad_row_changes_nothing(self):
        with self.assertRaises(charges.ChargeError):
            charges.import_csv(csv_of([(OTHER, 100000, 199999, 1500), (CODE, 'x', 5, 1)]), self.user)
        self.assertFalse(MuseChangeRequest.objects.filter(kind=MuseChangeKind.FSP_CHARGES).exists())

    def test_import_overlapping_the_current_bands_is_refused(self):
        with self.assertRaises(charges.ChargeError):
            charges.import_csv(csv_of([(CODE, 5000, 20000, 900)]), self.user)
        self.assertFalse(self.pending().exists())

    def test_approval_summary_lists_the_bands(self):
        summary = setup.describe_change(MuseChangeKind.FSP_CHARGES, CODE,
                                        {'bands': charges.canonical_bands(BANDS)},
                                        {'bands': charges.canonical_bands(BANDS[:1])})
        self.assertIn(CODE, summary['title'])
        self.assertEqual(summary['changes'][0]['after'], '1–9,999: 500')


class FspNamesApprovalTest(TestCase):
    def setUp(self):
        from core.models import User
        from tasaf_payment.models import FspMapping
        self.user = User.objects.filter(username='Admin').first()
        if self.user is None:
            self.skipTest('needs the Admin user')
        engine = mock.patch('approval.services.ApprovalService.request_approval', return_value={'success': True})
        engine.start()
        self.addCleanup(engine.stop)
        setup.apply_names(CODE, ['Test Bank', 'TB'], self.user)
        FspMapping(fsp_name='Other Bank', fsp_code=OTHER).save(username=self.user.username)

    def pending(self):
        return MuseChangeRequest.objects.filter(kind=MuseChangeKind.FSP_NAMES, fsp_code=CODE,
                                                status=MuseChangeStatus.PENDING)

    def test_name_change_waits_for_approval_then_applies(self):
        self.assertEqual(setup.save_names(self.user, CODE, ['Test Bank', 'Test Bank Ltd']), charges.PROPOSED)
        self.assertEqual(setup.fsp_names(CODE), ['TB', 'Test Bank'])
        change = self.pending().get()
        self.assertEqual(change.current['names'], ['TB', 'Test Bank'])
        setup._apply(change, self.user)
        self.assertEqual(setup.fsp_names(CODE), ['Test Bank', 'Test Bank Ltd'])

    def test_same_names_in_another_spelling_open_nothing(self):
        self.assertEqual(setup.save_names(self.user, CODE, ['TB', ' Test  Bank ', 'test bank']), charges.UNCHANGED)
        self.assertFalse(self.pending().exists())

    def test_a_name_of_another_fsp_is_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.save_names(self.user, CODE, ['Test Bank', 'other bank'])

    def test_without_approval_applies_at_once(self):
        with mock.patch.object(TasafPaymentConfig, 'charges_require_approval', False):
            self.assertEqual(setup.save_names(self.user, CODE, ['TB']), charges.APPLIED)
        self.assertEqual(setup.fsp_names(CODE), ['TB'])

    def test_a_new_fsp_waiting_for_its_name_is_listed(self):
        setup.save_names(self.user, 'TSTNEW', ['Brand New Bank'])
        row = next(p for p in setup.provider_list() if p['fsp_code'] == 'TSTNEW')
        self.assertTrue(row['names_pending'])
