from unittest import mock

from django.test import TestCase

from tasaf_payment import prefixes
from tasaf_payment.gepg.sender import WITHDRAWN_KEY, GepgVerificationSender
from tasaf_payment.models import (
    FspProfile, GepgVerificationRequest, MuseVerificationRecord, PaymentAccount, VerificationStatus,
)
from tasaf_payment.muse import setup as muse_setup

ACK = {'published': True, 'ok': True, 'status_code': 200, 'request_id': 'R1',
       'esb_body': {'gepgAccVerifSubReqAck': {'AccVerifStsCode': '7101', 'AccVerifStsDesc': 'Successful'}}}
CODES = {'Halopesa': 'HALOPESA', 'Airtel Money': 'AIRTELMONEY'}


class _Providers(TestCase):
    def setUp(self):
        FspProfile(fsp_code='HALOPESA', bank_name='HALOTEL', fsp_type='MOBILE', bic='HALOTZTX',
                   mobile_prefixes=['061', '062']).save(username='Admin')
        FspProfile(fsp_code='AIRTELMONEY', bank_name='AIRTEL', fsp_type='MOBILE', bic='AMTLTZTX',
                   mobile_prefixes=['068', '069', '078']).save(username='Admin')
        p = mock.patch('tasaf_payment.charges.resolve_fsp_code', side_effect=lambda name: CODES.get(name))
        p.start()
        self.addCleanup(p.stop)

    def account(self, number, fsp_name='Halopesa'):
        return PaymentAccount(fsp_type='MOBILE', fsp_name=fsp_name, account_number=number)


class PrefixRulesTest(_Providers):
    def test_prefixes_are_normalised(self):
        self.assertEqual(prefixes.normalise_list('78, 068 25569;078'), ['078', '068', '069'])
        self.assertEqual(prefixes.invalid(['078', '088', '0781', '07812']), ['088', '07812'])

    def test_number_of_another_provider_is_a_mismatch(self):
        problem = prefixes.problem(self.account('255782191101'))
        self.assertEqual((problem['code'], problem['belongs_to'], problem['prefix']),
                         (prefixes.PROVIDER_MISMATCH, 'AIRTELMONEY', '078'))

    def test_own_unknown_and_bank_numbers_pass(self):
        self.assertIsNone(prefixes.problem(self.account('255612191101')))
        self.assertIsNone(prefixes.problem(self.account('255632191101')))
        self.assertIsNone(prefixes.problem(PaymentAccount(fsp_type='BANK', fsp_name='NMB', account_number='123')))

    def test_malformed_number_is_invalid_even_with_the_check_off(self):
        with mock.patch.object(prefixes, 'check_enabled', return_value=False):
            self.assertEqual(prefixes.problem(self.account('2557821911'))['code'], prefixes.INVALID_MOBILE)
            self.assertIsNone(prefixes.problem(self.account('255782191101')))

    def test_longest_prefix_wins(self):
        FspProfile(fsp_code='TESTNET', bank_name='T', fsp_type='MOBILE', bic='TESTTZTZ',
                   mobile_prefixes=['0782']).save(username='Admin')
        self.assertEqual(prefixes.owner('255782191101'), ('TESTNET', '0782'))

    def test_profile_refuses_a_prefix_owned_elsewhere_and_a_bad_one(self):
        with self.assertRaisesMessage(muse_setup.SetupError, '078 (AIRTELMONEY)'):
            muse_setup.normalise_profile('HALOPESA', 'HALOTEL', 'MOBILE', 'HALOTZTX', ['061', '078'])
        with self.assertRaisesMessage(muse_setup.SetupError, '088'):
            muse_setup.normalise_profile('HALOPESA', 'HALOTEL', 'MOBILE', 'HALOTZTX', ['088'])
        code, values = muse_setup.normalise_profile('HALOPESA', 'HALOTEL', 'MOBILE', 'HALOTZTX', '61 62 63')
        self.assertEqual(values['mobile_prefixes'], ['061', '062', '063'])

    def test_profile_keeps_prefixes_when_none_are_given_and_bank_has_none(self):
        _, values = muse_setup.normalise_profile('HALOPESA', 'HALOTEL', 'MOBILE', 'HALOTZTX')
        self.assertEqual(values['mobile_prefixes'], ['061', '062'])
        _, values = muse_setup.normalise_profile('NMB', 'NMB', 'BANK', 'NMIBTZTZ', ['061'])
        self.assertEqual(values['mobile_prefixes'], [])


    def test_an_approved_change_saves_the_prefixes(self):
        from types import SimpleNamespace
        from core.models import User
        from tasaf_payment.models import MuseChangeKind
        _, values = muse_setup.normalise_profile('HALOPESA', 'HALOTEL', 'MOBILE', 'HALOTZTX', ['061', '062', '063'])
        muse_setup._apply(SimpleNamespace(kind=MuseChangeKind.FSP_PROFILE, fsp_code='HALOPESA', proposed=values),
                          User.objects.filter(username='Admin').first())
        self.assertEqual(FspProfile.objects.get(fsp_code='HALOPESA', is_deleted=False).mobile_prefixes,
                         ['061', '062', '063'])
        summary = muse_setup.describe_change(MuseChangeKind.FSP_PROFILE, 'HALOPESA', {'mobile_prefixes': ['061']},
                                             {'mobile_prefixes': ['061', '063']})
        self.assertEqual(summary['changes'][0]['after'], '061, 063')


class WithdrawAtSendTest(_Providers):
    def setUp(self):
        super().setUp()
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        accounts = list(PaymentAccount.objects.filter(is_deleted=False, group_beneficiary__isnull=False)
                        .order_by('id')[:3])
        if self.user is None or len(accounts) < 3:
            self.skipTest('needs the Admin user and three payment accounts with a household')
        for account, number in zip(accounts, ('255612191101', '255782191101', '25561219')):
            PaymentAccount.objects.filter(id=account.id).update(
                fsp_name='Halopesa', fsp_type='MOBILE', account_number=number,
                verification_status=VerificationStatus.PENDING, json_ext={})
        self.ids = [a.id for a in accounts]
        for target, kwargs in (('tasaf_payment.gepg.sender.blocked', {'return_value': None}),
                               ('tasaf_payment.gepg.setup.settings_values',
                                {'return_value': {'sp_code': 'SP991', 'sp_sys_id': 'TASAF001'}})):
            p = mock.patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_wrong_and_malformed_numbers_are_withdrawn_the_rest_sent(self):
        producer = mock.Mock(**{'publish.return_value': ACK})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            result = GepgVerificationSender(self.user, sleep=lambda s: None).dispatch(self.ids)
        self.assertEqual((result['count'], result['withdrawn']), (1, 2))
        request = GepgVerificationRequest.objects.get()
        self.assertEqual([i.acc_num for i in request.items.all()], ['255612191101'])
        good, wrong, bad = (PaymentAccount.objects.get(id=i) for i in self.ids)
        self.assertEqual(good.verification_status, VerificationStatus.PENDING_MUSE)
        self.assertEqual(wrong.verification_status, VerificationStatus.FAILED)
        self.assertEqual(wrong.json_ext[WITHDRAWN_KEY]['code'], prefixes.PROVIDER_MISMATCH)
        self.assertEqual(bad.json_ext[WITHDRAWN_KEY]['code'], prefixes.INVALID_MOBILE)
        record = MuseVerificationRecord.objects.filter(payment_account=wrong).latest('received_at')
        self.assertIn('is AIRTELMONEY (078)', record.failure_reason)

    def test_preview_counts_the_withdrawn(self):
        preview = GepgVerificationSender(self.user).preview(self.ids)
        self.assertEqual((preview['accounts'], preview['withdrawn']), (3, 2))
        self.assertEqual(sum(m['count'] for m in preview['messages']), 1)

    def test_a_corrected_account_sends_and_loses_the_marker(self):
        producer = mock.Mock(**{'publish.return_value': ACK})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            sender = GepgVerificationSender(self.user, sleep=lambda s: None)
            sender.dispatch([self.ids[1]])
            PaymentAccount.objects.filter(id=self.ids[1]).update(fsp_name='Airtel Money')
            GepgVerificationSender(self.user, sleep=lambda s: None).dispatch([self.ids[1]])
        account = PaymentAccount.objects.get(id=self.ids[1])
        self.assertEqual(account.verification_status, VerificationStatus.PENDING_MUSE)
        self.assertNotIn(WITHDRAWN_KEY, account.json_ext)
