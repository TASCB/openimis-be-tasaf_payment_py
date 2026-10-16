from unittest import mock

from django.db.models import Count, Q
from django.test import TestCase

from tasaf_payment import muse_inbound, muse_sender
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
            mock.patch.object(muse_sender, '_config', side_effect=lambda name, default: default),
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
