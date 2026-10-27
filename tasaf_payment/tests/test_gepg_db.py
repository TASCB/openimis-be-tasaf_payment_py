from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from tasaf_payment.gepg import inbound, message as gm, setup as gepg_setup, sweeper
from tasaf_payment.gepg.sender import GepgVerificationSender
from tasaf_payment.models import (
    FspProfile, GepgItemStatus, GepgRequestStatus, GepgSettings, GepgVerificationRequest, MuseChangeKind,
    PaymentAccount, VerificationStatus,
)

SETTINGS = {'sp_code': 'SP991', 'sp_sys_id': 'TASAF001'}


def ack(code='7101', desc='Successful'):
    return {'published': True, 'ok': True, 'status_code': 200, 'request_id': 'R1',
            'esb_body': {gm.REQUEST_ACK: {'AccVerifId': 'x', 'AccVerifStsCode': code, 'AccVerifStsDesc': desc}}}


class GepgVerificationDbTest(TestCase):
    def setUp(self):
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        self.accounts = list(PaymentAccount.objects.filter(
            is_deleted=False, group_beneficiary__isnull=False).order_by('id')[:2])
        if self.user is None or len(self.accounts) < 2:
            self.skipTest('needs the Admin user and two payment accounts with a household')
        for n, account in enumerate(self.accounts):
            PaymentAccount.objects.filter(id=account.id).update(
                fsp_name='GEPGTEST', fsp_type='MOBILE', account_number=f'25576748267{n}',
                verification_status=VerificationStatus.FAILED if n else VerificationStatus.PENDING, json_ext={})
        FspProfile(fsp_code='GEPGTEST', bank_name='GePG test', fsp_type='MOBILE',
                   bic='AMTLTZTX', mobile_prefixes=['0767']).save(username='Admin')
        self.ids = [a.id for a in self.accounts]
        for target, kwargs in (('tasaf_payment.gepg.sender.blocked', {'return_value': None}),
                               ('tasaf_payment.gepg.setup.settings_values', {'return_value': SETTINGS}),
                               ('tasaf_payment.charges.resolve_fsp_code',
                                {'side_effect': lambda name: 'GEPGTEST' if name == 'GEPGTEST' else None})):
            p = mock.patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def dispatch(self, response):
        producer = mock.Mock(**{'publish.return_value': response})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            result = GepgVerificationSender(self.user, sleep=lambda s: None).dispatch(self.ids)
        return result, producer

    def statuses(self):
        return [PaymentAccount.objects.get(id=i).verification_status for i in self.ids]

    def test_accepted_request_leaves_accounts_pending(self):
        result, producer = self.dispatch(ack())
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 2)
        request = GepgVerificationRequest.objects.get()
        self.assertEqual(request.status, GepgRequestStatus.ACCEPTED)
        wire = producer.publish.call_args[0][1]
        self.assertTrue(wire.startswith('<Gepg><gepgAccVerifSubReq><SpCode>SP991</SpCode>'))
        self.assertEqual(producer.publish.call_args[1]['data_format'].value, 'xml')
        body = gm.load(wire)['Gepg'][gm.REQUEST]
        self.assertEqual((body['SpCode'], body['Bic'], len(body['AccVerifItems'])), ('SP991', 'AMTLTZTX', 2))
        self.assertEqual(request.request_body, wire)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING_MUSE] * 2)
        sent = PaymentAccount.objects.get(id=self.ids[0]).json_ext['verification_sent']
        self.assertEqual(sent['gepg'], request.acc_verif_id)
        self.assertNotIn('after_change', sent)

    def test_a_send_after_a_change_is_marked(self):
        producer = mock.Mock(**{'publish.return_value': ack()})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            GepgVerificationSender(self.user, sleep=lambda s: None).dispatch(self.ids, after_change=True)
        self.assertTrue(GepgVerificationRequest.objects.get().after_change)
        sent = PaymentAccount.objects.get(id=self.ids[0]).json_ext['verification_sent']
        self.assertTrue(sent['after_change'])

    def test_service_code_goes_as_a_header_through_govesb(self):
        with mock.patch('tasaf_payment.gepg.setup.settings_values',
                        return_value={**SETTINGS, 'service_code': 'SRVC1'}):
            result, producer = self.dispatch(ack())
        self.assertEqual(producer.publish.call_args[1]['extra_headers'],
                         {'service-code': 'SRVC1', 'Gepg-Code': 'SP991'})
        self.assertEqual(result['count'], 2)

    def test_no_service_code_still_sends_gepg_code(self):
        result, producer = self.dispatch(ack())
        self.assertEqual(producer.publish.call_args[1]['extra_headers'], {'Gepg-Code': 'SP991'})

    def test_json_format_sends_the_dict(self):
        with mock.patch('tasaf_payment.apps.TasafPaymentConfig.gepg_message_format', 'JSON'):
            _, producer = self.dispatch(ack())
        self.assertIn(gm.REQUEST, producer.publish.call_args[0][1])
        self.assertEqual(producer.publish.call_args[1]['data_format'].value, 'json')

    def test_xml_ack_is_read(self):
        reply = {**ack(), 'esb_body': '<Gepg><gepgAccVerifSubReqAck><AccVerifId>x</AccVerifId>'
                                      '<AccVerifStsCode>7101</AccVerifStsCode><AccVerifStsDesc>Ok'
                                      '</AccVerifStsDesc></gepgAccVerifSubReqAck></Gepg>'}
        self.dispatch(reply)
        self.assertEqual(GepgVerificationRequest.objects.get().status, GepgRequestStatus.ACCEPTED)

    def test_xml_answer_through_the_endpoint(self):
        from django.test import RequestFactory, override_settings
        from tasaf_payment.views import GepgMessageView
        self.dispatch(ack())
        request = GepgVerificationRequest.objects.get()
        item = request.items.order_by('id').first()
        answer = gm.as_xml({gm.RESPONSE: {'AccVerifId': request.acc_verif_id, 'Bic': request.bic, 'AccVerifItems': [
            {'AccVerifItemId': item.item_id, 'AccNum': item.acc_num, 'AccName': 'Someone Else',
             'AccStsCode': '7101', 'AccStsDesc': 'Successful'}]}})
        envelope = f'<esbrequest><data><esbBody>{answer}</esbBody></data></esbrequest>'
        http = RequestFactory().post('/api/tasaf_payment/gepg/message/', data=envelope,
                                     content_type='application/xml')
        with override_settings(ESB=None):
            response = GepgMessageView.as_view()(http)
        text = response.content.decode()
        self.assertEqual(response['Content-Type'], 'application/xml')
        self.assertIn('<success>true</success>', text)
        self.assertIn(f'<gepgAccVerifSubRespAck><AccVerifId>{request.acc_verif_id}</AccVerifId>'
                      '<AccVerifStsCode>7101</AccVerifStsCode>', text)
        item.refresh_from_db()
        self.assertEqual(item.status, GepgItemStatus.ANSWERED)

        from tasaf_payment.models import GepgMessageLog
        log = GepgMessageLog.objects.get()
        self.assertEqual((log.acc_verif_id, log.data_format, log.accepted), (request.acc_verif_id, 'XML', True))
        self.assertIn('Someone Else', log.body)
        self.assertIn('<AccVerifStsCode>7101</AccVerifStsCode>', log.reply)

        unknown = envelope.replace(request.acc_verif_id, 'TVNOTOURS')
        with override_settings(ESB=None):
            GepgMessageView.as_view()(RequestFactory().post('/api/tasaf_payment/gepg/message/', data=unknown,
                                                            content_type='application/xml'))
        log = GepgMessageLog.objects.order_by('-id').first()
        self.assertEqual((log.acc_verif_id, log.accepted), ('TVNOTOURS', False))

    def test_rejected_request_restores_previous_statuses(self):
        result, _ = self.dispatch(ack('7201', 'Invalid SpCode'))
        self.assertFalse(result['success'])
        self.assertEqual(GepgVerificationRequest.objects.get().status, GepgRequestStatus.REJECTED)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])

    def test_govesb_refusal_restores_accounts(self):
        result, producer = self.dispatch({'published': True, 'ok': False, 'status_code': 400, 'error': '710'})
        self.assertFalse(result['success'])
        self.assertEqual(producer.publish.call_count, 1)
        self.assertEqual(GepgVerificationRequest.objects.get().status, GepgRequestStatus.FAILED)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])

    def test_govesb_failure_carrying_a_gepg_ack_records_gepg_code(self):
        reply = {**ack('7201', 'Error :Connection refused'), 'ok': False, 'error': 'GovESB refused: failed'}
        result, producer = self.dispatch(reply)
        self.assertFalse(result['success'])
        self.assertEqual(producer.publish.call_count, 1)
        request = GepgVerificationRequest.objects.get()
        self.assertEqual((request.status, request.status_code), (GepgRequestStatus.REJECTED, '7201'))
        self.assertIn('Connection refused', request.status_desc)
        self.assertIsNotNone(request.ack_body)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])

    def test_server_errors_are_retried(self):
        result, producer = self.dispatch({'published': True, 'ok': False, 'status_code': 503})
        self.assertEqual(producer.publish.call_count, 3)

    def test_blocked_sends_nothing(self):
        with mock.patch('tasaf_payment.gepg.sender.blocked', return_value='GovESB is not configured'):
            result = GepgVerificationSender(self.user).dispatch(self.ids)
        self.assertFalse(result['success'])
        self.assertFalse(GepgVerificationRequest.objects.exists())

    def test_fsp_without_bic_is_skipped(self):
        FspProfile.objects.filter(fsp_code='GEPGTEST').update(bic='')
        result, producer = self.dispatch(ack())
        self.assertEqual((result['count'], result['skipped']), (0, 2))
        producer.publish.assert_not_called()

    def respond(self, request, entries):
        payload = {gm.RESPONSE: {'AccVerifId': request.acc_verif_id, 'Bic': request.bic,
                                 'AccVerifItems': entries}}
        return inbound.handle(payload)

    def test_answer_verifies_by_name_and_fails_empty_names(self):
        self.dispatch(ack())
        request = GepgVerificationRequest.objects.get()
        first, second = request.items.order_by('id')
        ok, reply = self.respond(request, [
            {'AccVerifItemId': first.item_id, 'AccNum': first.acc_num, 'AccName': first.expected_name,
             'AccStsCode': '7101', 'AccStsDesc': 'Successful'},
            {'AccVerifItemId': second.item_id, 'AccNum': second.acc_num, 'AccName': '',
             'AccStsCode': '7240', 'AccStsDesc': 'Account not found'},
        ])
        self.assertTrue(ok)
        self.assertEqual(reply[gm.RESPONSE_ACK]['AccVerifStsCode'], '7101')
        request.refresh_from_db()
        self.assertEqual(request.status, GepgRequestStatus.ANSWERED)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.decision, 'FAILED')
        self.assertEqual(PaymentAccount.objects.get(id=second.payment_account_id).verification_status,
                         VerificationStatus.FAILED)
        if first.expected_name:
            self.assertIn(first.decision, ('VERIFIED', 'MANUAL'))

        again, reply = self.respond(request, [
            {'AccVerifItemId': second.item_id, 'AccNum': second.acc_num, 'AccName': 'Someone Else',
             'AccStsCode': '7101', 'AccStsDesc': 'Successful'}])
        self.assertTrue(again)
        second.refresh_from_db()
        self.assertEqual(second.acc_name, '')

    def test_a_provider_that_did_not_answer_leaves_the_account_unchecked(self):
        self.dispatch(ack())
        request = GepgVerificationRequest.objects.get()
        first, second = request.items.order_by('id')
        ok, _ = self.respond(request, [
            {'AccVerifItemId': i.item_id, 'AccNum': i.acc_num, 'AccName': '',
             'AccStsCode': '7355', 'AccStsDesc': 'No response from payment service provider'}
            for i in (first, second)])
        self.assertTrue(ok)
        first.refresh_from_db()
        self.assertEqual((first.status, first.decision, first.acc_sts_code), (GepgItemStatus.ANSWERED, 'UNCHECKED', '7355'))
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])
        from tasaf_payment.gepg.history import request_rows
        self.assertEqual(request_rows()[0]['unchecked'], 2)

    def test_an_answer_arriving_before_the_ack_is_kept(self):
        def publish(*args, **kwargs):
            request = GepgVerificationRequest.objects.get()
            self.respond(request, [{'AccVerifItemId': i.item_id, 'AccNum': i.acc_num, 'AccName': 'Someone Else',
                                    'AccStsCode': '7101', 'AccStsDesc': 'Successful'}
                                   for i in request.items.order_by('id')])
            return ack()

        producer = mock.Mock(**{'publish.side_effect': publish})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            GepgVerificationSender(self.user, sleep=lambda s: None).dispatch(self.ids)
        request = GepgVerificationRequest.objects.get()
        self.assertEqual((request.status, request.status_code), (GepgRequestStatus.ANSWERED, '7101'))
        self.assertIsNotNone(request.answered_at)
        self.assertIsNotNone(request.response_body)
        self.assertIsNotNone(request.ack_body)
        self.assertEqual(request.items.filter(status=GepgItemStatus.ANSWERED).count(), 2)

    def test_an_answer_signals_once_with_the_decisions(self):
        from tasaf_payment.events import gepg_answered
        receiver = mock.Mock()
        gepg_answered.connect(receiver, dispatch_uid='test.gepg_answered')
        self.addCleanup(gepg_answered.disconnect, dispatch_uid='test.gepg_answered')
        self.dispatch(ack())
        request = GepgVerificationRequest.objects.get()
        first, second = request.items.order_by('id')
        with self.captureOnCommitCallbacks(execute=True):
            self.respond(request, [{'AccVerifItemId': second.item_id, 'AccNum': second.acc_num, 'AccName': '',
                                    'AccStsCode': '7240', 'AccStsDesc': 'Account not found'}])
        receiver.assert_called_once()
        self.assertEqual(receiver.call_args.kwargs['counts'], {'FAILED': 1, 'PENDING': 1})

    def test_the_send_counts_are_kept_for_the_pop_up(self):
        from django.core.cache import cache
        from django.test import override_settings
        from tasaf_payment.gepg import outcome
        with override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}):
            cache.clear()
            result, _ = self.dispatch(ack())
            outcome.remember('cmid-1', result)
            kept = outcome.recall('cmid-1')
        self.assertEqual((kept['count'], kept['batches'], kept['refused'], kept['withdrawn']),
                         (result['count'], 1, 0, 0))

    def test_unknown_or_malformed_answers_are_not_acknowledged(self):
        self.assertFalse(inbound.handle({gm.RESPONSE: {'AccVerifId': 'NOPE'}})[0])
        self.assertFalse(inbound.handle({'message': {}})[0])

    def test_mismatched_account_number_is_ignored(self):
        self.dispatch(ack())
        request = GepgVerificationRequest.objects.get()
        item = request.items.first()
        ok, _ = self.respond(request, [{'AccVerifItemId': item.item_id, 'AccNum': '255700000000',
                                        'AccName': 'X', 'AccStsCode': '7101', 'AccStsDesc': ''}])
        self.assertTrue(ok)
        item.refresh_from_db()
        self.assertEqual(item.status, GepgItemStatus.PENDING)

    def test_sweeper_releases_unanswered_requests(self):
        self.dispatch(ack())
        GepgVerificationRequest.objects.update(sent_at=datetime.now(tz=timezone.utc) - timedelta(hours=72))
        self.assertEqual(len(sweeper.expire(48)), 1)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING_MUSE] * 2)
        sweeper.expire(48, apply=True)
        self.assertEqual(GepgVerificationRequest.objects.get().status, GepgRequestStatus.EXPIRED)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])

    def test_an_unanswered_request_is_announced_once(self):
        self.dispatch(ack())
        GepgVerificationRequest.objects.update(sent_at=datetime.now(tz=timezone.utc) - timedelta(minutes=45))
        self.assertEqual(len(sweeper.remind(60)), 0)
        with mock.patch('tasaf_payment.events.emit_gepg_unanswered') as emit:
            self.assertEqual(len(sweeper.remind(30, apply=True)), 1)
            self.assertEqual(len(sweeper.remind(30, apply=True)), 0)
        emit.assert_called_once()
        request = GepgVerificationRequest.objects.get()
        self.assertIsNotNone(request.reminded_at)
        self.assertEqual(request.status, GepgRequestStatus.ACCEPTED)

    def test_command_takes_a_zero_deadline(self):
        from io import StringIO
        from django.core.management import call_command
        self.dispatch(ack())
        GepgVerificationRequest.objects.update(sent_at=datetime.now(tz=timezone.utc) - timedelta(minutes=5))
        out = StringIO()
        call_command('expire_gepg_verifications', hours=0, warn_minutes=0, stdout=out)
        self.assertIn('1 request(s) older than 0 h', out.getvalue())

    def test_a_request_without_an_ack_can_be_released_and_sent_fresh(self):
        self.dispatch({'published': True, 'ok': True, 'status_code': 200, 'request_id': 'R1', 'esb_body': {}})
        stuck = GepgVerificationRequest.objects.get()
        self.assertEqual(stuck.status, GepgRequestStatus.SENT)
        self.assertEqual(sweeper.release(stuck.acc_verif_id, self.user), 2)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, GepgRequestStatus.EXPIRED)
        self.assertIn('Released by Admin', stuck.status_desc)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING, VerificationStatus.FAILED])
        with self.assertRaises(ValueError):
            sweeper.release(stuck.acc_verif_id, self.user)
        producer = mock.Mock(**{'publish.return_value': ack()})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            self.assertTrue(GepgVerificationSender(self.user).resend(stuck.acc_verif_id)['success'])
        fresh = GepgVerificationRequest.objects.exclude(id=stuck.id).get()
        self.assertNotEqual(fresh.acc_verif_id, stuck.acc_verif_id)
        self.assertEqual(self.statuses(), [VerificationStatus.PENDING_MUSE] * 2)

    def test_resend_creates_a_new_request(self):
        self.dispatch(ack('7201'))
        first = GepgVerificationRequest.objects.get()
        producer = mock.Mock(**{'publish.return_value': ack()})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            result = GepgVerificationSender(self.user).resend(first.acc_verif_id)
        self.assertTrue(result['success'])
        self.assertEqual(GepgVerificationRequest.objects.exclude(id=first.id).get().status,
                         GepgRequestStatus.ACCEPTED)
        with self.assertRaises(ValueError):
            GepgVerificationSender(self.user).resend(GepgVerificationRequest.objects.exclude(id=first.id)
                                                     .get().acc_verif_id)


class GepgSettingsDbTest(TestCase):
    def test_approved_change_is_applied_to_gepg_settings(self):
        from core.models import User
        user = User.objects.filter(username='Admin').first()
        if user is None:
            self.skipTest('needs the Admin user')
        change = SimpleNamespace(kind=MuseChangeKind.GEPG_SETTINGS,
                                 proposed=gepg_setup.normalise_settings({
                                     'sp_code': 'sp991', 'sp_sys_id': 'tasaf001',
                                     'service_code': ' SRVC1 '}))
        gepg_setup.apply(change, user)
        s = GepgSettings.objects.filter(is_deleted=False).order_by('-date_created').first()
        self.assertEqual((s.sp_code, s.sp_sys_id, s.service_code), ('SP991', 'TASAF001', 'SRVC1'))

    def test_invalid_values_are_refused(self):
        from tasaf_payment.muse.setup import SetupError
        with self.assertRaises(SetupError):
            gepg_setup.normalise_settings({'sp_code': 'TOOLONG'})
        with self.assertRaises(SetupError):
            gepg_setup.normalise_settings({'service_code': 'has space'})
