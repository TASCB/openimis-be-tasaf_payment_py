from unittest import mock

from django.db.models import Count, Q
from django.test import TestCase

from tasaf_payment.muse import inbound as muse_inbound, sender as muse_sender
from tasaf_payment.models import Paylist, PaylistItemStatus, PaylistStatus, PaymentDestination
from tasaf_payment.services import PaylistService

OK = {'published': True, 'ok': True, 'status_code': 200, 'request_id': 'R1', 'esb_body': {}}


def verdict(msg_id, status, desc=''):
    return {'message': {
        'messageHeader': {'msgId': f'muse-{status}', 'sender': 'MUSE', 'receiver': 'TASAFMIS',
                          'createdAt': '2026-10-16T11:06:27', 'messageType': 'BULK_PAYMENT_STATUS'},
        'messageDetails': {'status': status, 'orgMsgId': msg_id, 'endToEndId': None,
                           'statusDesc': desc, 'orgReferenceNo': 'TP-TEST'}},
        'digitalSignature': 'x'}


class MuseSendRaceDbTest(TestCase):
    def setUp(self):
        from core.models import User
        pending = Q(items__status=PaylistItemStatus.PENDING, items__is_deleted=False)
        self.paylist = (Paylist.objects.filter(is_deleted=False)
                        .annotate(n=Count('items', filter=pending)).filter(n__gt=0).order_by('-n', 'id').first())
        self.user = User.objects.filter(username='Admin').first()
        if self.paylist is None or self.user is None:
            self.skipTest('needs a paylist with pending payees and the Admin user')
        Paylist.objects.filter(id=self.paylist.id).update(
            status=PaylistStatus.APPROVED, destination=PaymentDestination.MUSE,
            muse_msg_id=None, muse_batch_reference=None, submitted_at=None, json_ext={})
        no_rows = mock.Mock(**{'count.return_value': 0})
        patches = [
            mock.patch('tasaf_payment.services._wrong_type_refusal', return_value=None),
            mock.patch('tasaf_payment.services.paid_elsewhere', return_value=no_rows),
            mock.patch('tasaf_payment.services.unpayable_items', return_value=no_rows),
            mock.patch.object(muse_sender.muse_message, 'check', return_value=[]),
            mock.patch.object(muse_sender, 'muse_config', side_effect=lambda name, default: default),
            mock.patch('coremis_app_integration.govesb.govesb_enabled', return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def submit(self, *replies, response=OK):
        def publish(topic, body, **kwargs):
            msg_id = body['message']['messageHeader']['msgId']
            for status, desc in replies:
                muse_inbound.handle(verdict(msg_id, status, desc))
            return response

        producer = mock.Mock(**{'publish.side_effect': publish})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer), \
                self.captureOnCommitCallbacks(execute=False):
            result = PaylistService(self.user)._submit(str(self.paylist.uuid))
        return result, Paylist.objects.get(id=self.paylist.id)

    def last_in_note(self):
        from muse_payment_adaptor.models import MuseTransactionLog
        return MuseTransactionLog.objects.filter(direction='IN').order_by('-id').first().error_message

    def test_accepted_during_send_is_kept(self):
        result, paylist = self.submit(('Accepted', 'Accepted Successfully in MUSE'))
        self.assertTrue(result['success'])
        self.assertEqual(paylist.status, PaylistStatus.ACCEPTED)
        self.assertEqual(paylist.muse_status_desc, 'Accepted Successfully in MUSE')
        self.assertIsNotNone(paylist.submitted_at)
        self.assertEqual(paylist.json_ext['muse_last_send']['outcome'], 'SENT')
        self.assertNotIn(muse_sender.SENDING_KEY, paylist.json_ext)
        self.assertNotIn(muse_sender.REPLY_DURING_SEND_KEY, paylist.json_ext)
        self.assertEqual(self.last_in_note(), 'applied during send (paylist was APPROVED)')

    def test_rejected_during_send_is_not_overwritten_by_submitted(self):
        _, paylist = self.submit(('Rejected', ''))
        self.assertEqual(paylist.status, PaylistStatus.REJECTED)
        self.assertEqual(len(paylist.json_ext['muse_rejections']), 1)

    def test_accepted_then_rejected_during_send_ends_rejected(self):
        _, paylist = self.submit(('Accepted', 'Accepted Successfully in MUSE'), ('Rejected', ''))
        self.assertEqual(paylist.status, PaylistStatus.REJECTED)

    def test_failed_post_after_a_reply_still_counts_as_submitted(self):
        refused = {'published': True, 'ok': False, 'status_code': 400, 'error': 'bad', 'esb_body': None}
        result, paylist = self.submit(('Accepted', 'ok'), response=refused)
        self.assertTrue(result['success'])
        self.assertEqual(paylist.status, PaylistStatus.ACCEPTED)

    def test_no_reply_during_send_is_plain_submitted(self):
        result, paylist = self.submit()
        self.assertTrue(result['success'])
        self.assertEqual(paylist.status, PaylistStatus.SUBMITTED)
        self.assertNotIn(muse_sender.SENDING_KEY, paylist.json_ext)
        self.assertNotIn(muse_sender.REPLY_DURING_SEND_KEY, paylist.json_ext)

    def test_failed_send_without_reply_stays_approved(self):
        refused = {'published': True, 'ok': False, 'status_code': 400, 'error': 'bad', 'esb_body': None}
        result, paylist = self.submit(response=refused)
        self.assertFalse(result['success'])
        self.assertEqual(paylist.status, PaylistStatus.APPROVED)


class SubmitQueueDbTest(TestCase):
    setUp = MuseSendRaceDbTest.setUp

    def queue(self):
        with mock.patch('tasaf_payment.tasks.submit_paylist_task.delay') as delay:
            result = PaylistService(self.user).submit(str(self.paylist.uuid))
        return result, delay, Paylist.objects.get(id=self.paylist.id)

    def test_submit_queues_and_holds_the_paylist(self):
        result, delay, paylist = self.queue()
        self.assertTrue(result['success'])
        self.assertEqual(result['outcome'], 'QUEUED')
        delay.assert_called_once()
        self.assertIn('muse_submit_lock', paylist.json_ext)
        self.assertEqual(paylist.json_ext['muse_last_submit']['outcome'], 'QUEUED')

    def test_second_submit_while_sending_is_refused(self):
        self.queue()
        result, delay, _ = self.queue()
        self.assertFalse(result['success'])
        self.assertIn('Already being sent', result['error'])
        delay.assert_not_called()

    def test_expired_lock_does_not_block(self):
        from tasaf_payment.services import SUBMIT_LOCK_KEY
        Paylist.objects.filter(id=self.paylist.id).update(
            json_ext={SUBMIT_LOCK_KEY: {'at': '2026-01-01T00:00:00+00:00', 'by': 'x'}})
        result, delay, _ = self.queue()
        self.assertTrue(result['success'])
        delay.assert_called_once()

    def test_send_releases_the_lock_and_builds_once(self):
        self.queue()
        producer = mock.Mock(**{'publish.return_value': OK})
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer), \
                mock.patch.object(muse_sender.muse_message, 'build',
                                  wraps=muse_sender.muse_message.build) as build, \
                self.captureOnCommitCallbacks(execute=False):
            result = PaylistService(self.user).send_now(str(self.paylist.uuid))
        paylist = Paylist.objects.get(id=self.paylist.id)
        self.assertTrue(result['success'])
        self.assertEqual(build.call_count, 1)
        self.assertEqual(paylist.status, PaylistStatus.SUBMITTED)
        self.assertNotIn('muse_submit_lock', paylist.json_ext)
        sent = producer.publish.call_args[0][1]['message']['messageHeader']
        self.assertEqual(sent['msgId'], paylist.muse_msg_id)
        self.assertEqual(sent['createdAt'], paylist.json_ext['muse_created_at'])


class InvalidHhidWithdrawDbTest(TestCase):
    setUp = MuseSendRaceDbTest.setUp
    submit = MuseSendRaceDbTest.submit

    def break_one_hhid(self, code):
        from individual.models import Group
        item = (self.paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING)
                .select_related('payment_account__group_beneficiary').order_by('id').first())
        group_id = item.payment_account.group_beneficiary.group_id
        Group.objects.filter(id=group_id).update(code=code)
        return item, group_id

    def test_payee_without_p3_hhid_is_withdrawn_and_the_rest_sent(self):
        from case_management.models import FollowUpRemark
        from tasaf_payment.models import PaylistItem
        pending = self.paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING).count()
        bad, group_id = self.break_one_hhid('P3-230111202t-86643914')
        others = self.paylist.items.filter(
            is_deleted=False, status=PaylistItemStatus.PENDING,
            payment_account__group_beneficiary__group_id=group_id).count()
        result, paylist = self.submit()

        self.assertTrue(result['success'], result)
        self.assertEqual(result['withdrawn_no_hhid'], others)
        self.assertEqual(paylist.status, PaylistStatus.SUBMITTED)
        item = PaylistItem.objects.get(id=bad.id)
        self.assertEqual(item.status, PaylistItemStatus.WITHDRAWN)
        self.assertEqual(item.json_ext['withdrawn_reason'], 'NO_P3_HHID')
        self.assertIn('P3-230111202t-86643914', item.return_reason)
        self.assertTrue(FollowUpRemark.objects.filter(paylist_item_id=bad.id, category='DATA_QUALITY').exists())
        self.assertEqual(paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING).count(),
                         pending - others)

    def test_database_filter_agrees_with_the_hhid_check(self):
        from tasaf_payment.muse.rejection import withdraw_without_payee_code
        from tasaf_payment.payee_code import has_p3_hhid
        expected = sum(1 for code in self.paylist.items.filter(
            is_deleted=False, status=PaylistItemStatus.PENDING).values_list(
            'payment_account__group_beneficiary__group__code', flat=True) if not has_p3_hhid(code))
        self.assertEqual(withdraw_without_payee_code(self.paylist, self.user), expected)


class MusePreviewSizeDbTest(TestCase):
    setUp = MuseSendRaceDbTest.setUp

    def test_large_paylist_previews_a_sample_with_full_totals(self):
        from tasaf_payment.muse import message
        full = message.preview(self.paylist, sample_rows=1)
        with mock.patch.object(message, 'PREVIEW_FULL_CHECK_MAX', 0):
            sample = message.preview(self.paylist, sample_rows=1)
        self.assertTrue(full['checked_all'])
        self.assertFalse(sample['checked_all'])
        self.assertEqual(sample['payments'], full['payments'])
        summary = lambda p: p['body']['requestdata']['message']['paymentSummary']  # noqa: E731
        self.assertEqual(summary(sample)['totalAmount'], summary(full)['totalAmount'])
        self.assertEqual(summary(sample)['noofTransaction'], full['payments'])
