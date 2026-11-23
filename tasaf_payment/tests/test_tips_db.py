import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import RequestFactory, TestCase, override_settings

from tasaf_payment.models import (
    FspProfile, GepgItemStatus, PaymentAccount, TipsLookupRequest, TipsRequestStatus, VerificationStatus,
)
from tasaf_payment.tips import message as tm, setup as tips_setup, sweeper
from tasaf_payment.tips.sender import TipsLookupSender

SETTINGS = {'sender': 'MOFPTZT0', 'receiver': 'TANZTZT0', 'summary_identifier': '20610004189', 'summary_fsp_code': '016',
            'fsp_codes': {'TIPSTEST': '501'}, 'verification_route': 'TIPS'}
OK = {'published': True, 'ok': True, 'status_code': 200, 'request_id': 'R1', 'esb_body': {}}


class TipsLookupDbTest(TestCase):
    def setUp(self):
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        self.accounts = list(PaymentAccount.objects.filter(
            is_deleted=False, group_beneficiary__isnull=False).order_by('id')[:2])
        if self.user is None or len(self.accounts) < 2:
            self.skipTest('needs the Admin user and two payment accounts with a household')
        for n, account in enumerate(self.accounts):
            PaymentAccount.objects.filter(id=account.id).update(
                fsp_name='TIPSTEST', fsp_type='MOBILE', account_number=f'076748267{n}',
                verification_status=VerificationStatus.FAILED if n else VerificationStatus.PENDING, json_ext={})
        FspProfile(fsp_code='TIPSTEST', bank_name='TIPS test', fsp_type='MOBILE',
                   bic='TIGOTZTX').save(username='Admin')
        self.ids = [a.id for a in self.accounts]
        self.codes = {tm.SINGLE: 'SINGLE1', tm.BULK: 'BULK1'}
        for target, kwargs in (('tasaf_payment.tips.sender.blocked', {'return_value': None}),
                               ('tasaf_payment.tips.setup.settings_values', {'return_value': SETTINGS}),
                               ('tasaf_payment.tips.setup.api_code', {'side_effect': lambda k: self.codes.get(k, '')}),
                               ('tasaf_payment.charges.resolve_fsp_code',
                                {'side_effect': lambda name: 'TIPSTEST' if name == 'TIPSTEST' else None})):
            p = mock.patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def dispatch(self, response, ids=None):
        producer = mock.Mock(**{'publish.return_value': response})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            result = TipsLookupSender(self.user, sleep=lambda s: None).dispatch(ids or self.ids)
        return result, producer

    def statuses(self):
        return [PaymentAccount.objects.get(id=i).verification_status for i in self.ids]

    def callback(self, details, msg_id='TPPS_1', org=None):
        from tasaf_payment.views import TipsMessageView
        message = {'messageHeader': {'sender': 'TANZTZT0', 'receiver': 'MOFPTZT0', 'messageType': tm.MESSAGE_TYPE,
                                     'msgId': msg_id},
                   'messageSummary': {'recordsReceived': len(details), **({'orgMsgId': org} if org else {})},
                   'messageDetails': details}
        http = RequestFactory().post('/api/tasaf_payment/tips/message/', data=json.dumps({'message': message}),
                                     content_type='application/json')
        with override_settings(ESB=None):
            response = TipsMessageView.as_view()(http)
        return json.loads(response.content)

    def test_bulk_lookup_is_sent_on_the_bulk_topic_without_headers(self):
        result, producer = self.dispatch(OK)
        self.assertEqual((result['success'], result['count']), (True, 2))
        request = TipsLookupRequest.objects.get()
        self.assertEqual((request.kind, request.status, request.tips_code),
                         (tm.BULK, TipsRequestStatus.ACCEPTED, '501'))
        topic, body = producer.publish.call_args[0]
        self.assertEqual(topic, tm.TOPICS[tm.BULK])
        self.assertNotIn('extra_headers', producer.publish.call_args[1])
        details = body['message']['messageDetails']
        self.assertEqual([d['identifier'] for d in details], ['255767482670', '255767482671'])
        self.assertEqual({d['identifierType'] for d in details}, {'MSISDN'})
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING_MUSE] * 2)
        sent = PaymentAccount.objects.get(id=self.ids[0]).json_ext['verification_sent']
        self.assertEqual(sent['tips'], request.msg_id)

    def test_one_account_goes_single_and_an_answer_in_the_reply_is_applied(self):
        expected = TipsLookupSender(self.user)
        result, producer = self.dispatch(OK, ids=self.ids[:1])
        request = TipsLookupRequest.objects.get()
        self.assertEqual(request.kind, tm.SINGLE)
        self.assertEqual(producer.publish.call_args[0][0], tm.TOPICS[tm.SINGLE])
        self.assertNotIn('extra_headers', producer.publish.call_args[1])
        item = request.items.get()
        TipsLookupRequest.objects.all().delete()
        reply = {**OK, 'esb_body': {'message': {'messageHeader': {'msgId': 'TPPS_9'}, 'messageSummary': {
            'identifierType': 'MSISDN', 'identifier': item.identifier, 'fspId': '501',
            'fullName': item.expected_name, 'lookupId': 'L1'}}}}
        PaymentAccount.objects.filter(id=self.ids[0]).update(verification_status=VerificationStatus.PENDING)
        self.dispatch(reply, ids=self.ids[:1])
        request = TipsLookupRequest.objects.get()
        self.assertEqual(request.status, TipsRequestStatus.ANSWERED)
        item = request.items.get()
        self.assertEqual((item.status, item.lookup_id), (GepgItemStatus.ANSWERED, 'L1'))
        self.assertEqual(PaymentAccount.objects.get(id=self.ids[0]).verification_status,
                         VerificationStatus.VERIFIED)
        self.assertIsNotNone(expected)

    def test_a_single_reply_without_a_name_leaves_the_account_unchecked(self):
        reply = {**OK, 'esb_body': {'message': {
            'messageHeader': {'messageType': 'TIPSLOOKUP', 'msgId': 'FB1'},
            'messageSummary': {'identifier': '255767482670', 'payloadType': 'JSON', 'fspDestination': '501',
                               'identifierType': 'MSISDN'}}}}
        self.dispatch(reply, ids=self.ids[:1])
        request = TipsLookupRequest.objects.get()
        self.assertEqual(request.status, TipsRequestStatus.ANSWERED)
        self.assertEqual(request.items.get().decision, 'UNCHECKED')
        self.assertEqual(self.statuses()[0], VerificationStatus.PENDING)

    def test_callback_is_matched_by_identifier_and_acknowledged(self):
        self.dispatch(OK)
        request = TipsLookupRequest.objects.get()
        first, second = request.items.order_by('id')
        reply = self.callback([
            {'identifierType': 'MSISDN', 'identifier': '0' + first.identifier[3:], 'fspId': '501',
             'fullName': first.expected_name, 'lookupId': 'A'},
            {'fspDestination': '501', 'identifierType': 'MSISDN', 'identifier': second.identifier,
             'errorInformation': {'errorCode': '3204', 'errorDescription': 'Party not found'}},
            {'lookupId': 'only-an-id'},
        ])
        self.assertTrue(reply['data']['success'])
        summary = reply['data']['esbBody']['message']['messageSummary']
        self.assertEqual((summary['orgMsgId'], summary['status']), ('TPPS_1', 'RECEIVED'))
        request.refresh_from_db()
        self.assertEqual(request.status, TipsRequestStatus.ANSWERED)
        self.assertEqual(self.statuses(), [VerificationStatus.VERIFIED, VerificationStatus.FAILED])
        second.refresh_from_db()
        self.assertEqual((second.error_code, second.decision), ('3204', 'FAILED'))
        again = self.callback([{'identifierType': 'MSISDN', 'identifier': second.identifier, 'fspId': '501',
                                'fullName': 'Late answer'}])
        self.assertEqual(again['data']['esbBody']['message']['messageSummary']['status'], 'RECEIVED')
        self.assertEqual(self.statuses()[1], VerificationStatus.FAILED)

    def test_unknown_results_are_refused_in_the_ack(self):
        self.dispatch(OK)
        reply = self.callback([{'identifierType': 'MSISDN', 'identifier': '255700000000', 'fullName': 'Nobody'}])
        self.assertEqual(reply['data']['esbBody']['message']['messageSummary']['status'], 'REJECTED')
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING_MUSE] * 2)

    def test_govesb_refusal_releases_the_accounts(self):
        refused = {'published': True, 'ok': False, 'status_code': 200, 'request_id': 'R2', 'esb_body': {},
                   'error': 'GovESB refused: HTTP operation failed ... statusCode: 403 (errors [802])'}
        result, _ = self.dispatch(refused)
        self.assertFalse(result['success'])
        request = TipsLookupRequest.objects.get()
        self.assertEqual(request.status, TipsRequestStatus.FAILED)
        self.assertIn('403', request.status_desc)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])
        self.assertEqual(set(request.items.values_list('status', flat=True)), {GepgItemStatus.RELEASED})

    def test_provider_without_a_tips_code_is_skipped(self):
        with mock.patch('tasaf_payment.tips.setup.settings_values', return_value={**SETTINGS, 'fsp_codes': {}}):
            result, producer = self.dispatch(OK)
        self.assertEqual((result['count'], result['skipped']), (0, 2))
        producer.publish.assert_not_called()
        self.assertFalse(TipsLookupRequest.objects.exists())

    def test_unanswered_lookups_expire_and_go_back(self):
        self.dispatch(OK)
        TipsLookupRequest.objects.update(sent_at=datetime.now(tz=timezone.utc) - timedelta(hours=7))
        self.assertEqual(len(sweeper.expire(6)), 1)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING_MUSE] * 2)
        sweeper.expire(6, apply=True)
        self.assertEqual(TipsLookupRequest.objects.get().status, TipsRequestStatus.EXPIRED)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])

    def test_route_picks_the_sender(self):
        from tasaf_payment.services import verification_dispatcher
        self.assertIsInstance(verification_dispatcher(self.user), TipsLookupSender)
        with mock.patch('tasaf_payment.tips.setup.settings_values', return_value={}):
            from tasaf_payment.gepg.sender import GepgVerificationSender
            self.assertIsInstance(verification_dispatcher(self.user), GepgVerificationSender)

    def test_preview_shows_the_bulk_message(self):
        preview = TipsLookupSender(self.user).preview(self.ids)
        self.assertEqual((preview['channel'], preview['accounts']), ('TIPS', 2))
        self.assertEqual(len(preview['messages'][0]['body']['message']['messageDetails']), 2)
        self.assertEqual(preview['messages'][0]['problems'], [])


class TipsSettingsTest(TestCase):
    def test_settings_are_checked_and_normalised(self):
        from tasaf_payment.muse.setup import SetupError
        with self.assertRaises(SetupError):
            tips_setup.normalise_settings({'sender': 'MOFPTZT0', 'receiver': 'TANZTZT0', 'verification_route': 'TIPS'})
        values = tips_setup.normalise_settings({'sender': 'mofptzt0', 'receiver': 'TANZTZT0',
                                                'summary_identifier_type': 'bank', 'summary_identifier': '0011055818',
                                                'summary_fsp_code': '013', 'verification_route': 'tips',
                                                'fsp_codes': {'nmb': '016', 'crdb': ''}})
        self.assertEqual((values['sender'], values['verification_route']), ('MOFPTZT0', 'TIPS'))
        self.assertEqual(list(values['fsp_codes'].values()), ['016'])
        with self.assertRaises(SetupError):
            tips_setup.normalise_settings({'fsp_codes': {'NMB': '16'}, 'verification_route': 'MUSE'})
        with self.assertRaises(SetupError):
            tips_setup.normalise_settings({'summary_identifier_type': 'CARD', 'summary_identifier': 'a b'})

    def test_bulk_summary_account_is_required_before_sending(self):
        from tasaf_payment.tips import sender
        partial = {'sender': 'MOFPTZT0', 'receiver': 'TANZTZT0'}
        with mock.patch('tasaf_payment.tips.setup.govesb_on', return_value=True), \
                mock.patch('tasaf_payment.tips.setup.api_code', return_value='X'), \
                mock.patch('tasaf_payment.tips.setup.settings_values', return_value=partial):
            self.assertIn('bulk summary account', sender.blocked())
            self.assertEqual(tips_setup.readiness(providers=[])['settings_missing'],
                             ['summary_identifier_type', 'summary_identifier', 'summary_fsp_code'])

    def test_change_summary_names_the_tips_fields(self):
        from tasaf_payment.muse.setup import describe_change
        summary = describe_change('TIPS_SETTINGS', '', {}, {'fsp_codes': {'NMB': '016'}, 'verification_route': 'TIPS'})
        self.assertEqual((summary['title'], summary['integration']), ('TIPS settings', 'tips'))
        self.assertIn({'label': 'TIPS FSP codes', 'before': None, 'after': 'NMB: 016', 'changed': True},
                      summary['changes'])


class ProposeTipsSettingsCommandTest(TestCase):
    def test_proposes_codes_and_summary_without_applying(self):
        from io import StringIO
        from django.core.management import call_command
        from tasaf_payment.models import MuseChangeKind, MuseChangeRequest, MuseChangeStatus
        from tasaf_payment.muse.setup import provider_list
        if not any(p['fsp_code'] == 'TIGOPESA' for p in provider_list()):
            self.skipTest('needs the TIGOPESA provider')
        MuseChangeRequest.objects.filter(kind=MuseChangeKind.TIPS_SETTINGS).delete()
        before = tips_setup.settings_values()
        call_command('propose_tips_settings', maker='Admin', summary='BANK:0011055818:013', stdout=StringIO())
        change = MuseChangeRequest.objects.get(kind=MuseChangeKind.TIPS_SETTINGS, status=MuseChangeStatus.PENDING)
        self.assertEqual(change.proposed['fsp_codes']['TIGOPESA'], '501')
        self.assertEqual((change.proposed['summary_identifier'], change.proposed['summary_fsp_code']),
                         ('0011055818', '013'))
        self.assertEqual(change.proposed['verification_route'], before.get('verification_route') or 'GEPG')
        self.assertEqual(tips_setup.settings_values(), before)
