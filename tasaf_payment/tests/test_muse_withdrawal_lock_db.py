from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import TestCase

from core.models import User
from tasaf_payment.models import UNAPPLIED_ACTION_KEY, PaylistItem, PaylistItemStatus, UnappliedAction
from tasaf_payment.muse import withdrawal
from tasaf_payment.muse.sender import SEND


class MuseWithdrawalLockDbTest(TestCase):
    def setUp(self):
        self.user = User.objects.filter(username='Admin').first()
        items = list(PaylistItem.objects.filter(is_deleted=False, payment_account__isnull=False)[:2])
        if self.user is None or len(items) < 2:
            self.skipTest('need the Admin user and two payees')
        PaylistItem.objects.filter(status=PaylistItemStatus.UNAPPLIED).update(status=PaylistItemStatus.PENDING)
        for item in items:
            PaylistItem.objects.filter(pk=item.pk).update(
                status=PaylistItemStatus.UNAPPLIED,
                json_ext={UNAPPLIED_ACTION_KEY: {'action': UnappliedAction.WITHDRAW}})
        self.ids = [item.pk for item in items]
        self.producer = mock.Mock()
        patches = [
            mock.patch('tasaf_payment.muse.withdrawal._producer', return_value=self.producer),
            mock.patch('tasaf_payment.muse.withdrawal.build', return_value={}),
            mock.patch('tasaf_payment.muse.withdrawal._log'),
            mock.patch('tasaf_payment.muse.sender.muse_config', return_value=SEND),
            mock.patch('tasaf_payment.muse.sender.muse_reply', return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def states(self):
        return [((i.json_ext or {}).get(withdrawal.KEY) or {}).get('status')
                for i in PaylistItem.objects.filter(pk__in=self.ids)]

    def test_a_second_click_during_the_send_is_refused(self):
        def publish(*args, **kwargs):
            self.assertEqual(withdrawal.pending().count(), 0)
            with self.assertRaisesRegex(withdrawal.WithdrawalError, 'already being sent'):
                withdrawal.send(self.user)
            return {'ok': True, 'status_code': 200, 'esb_body': {}}

        self.producer.publish.side_effect = publish
        _, count = withdrawal.send(self.user)
        self.assertEqual(count, 2)
        self.assertEqual(self.producer.publish.call_count, 1)
        self.assertEqual(self.states(), [withdrawal.SENT, withdrawal.SENT])

    def test_a_failed_send_puts_the_payees_back(self):
        self.producer.publish.return_value = {'ok': False, 'status_code': 500, 'error': 'down'}
        with self.assertRaisesRegex(withdrawal.WithdrawalError, 'refused'):
            withdrawal.send(self.user)
        self.assertEqual(self.states(), [None, None])
        self.assertEqual(withdrawal.pending().count(), 2)

    def test_a_send_that_died_is_retried_after_the_lock_expires(self):
        old = (datetime.now(tz=timezone.utc) - timedelta(minutes=withdrawal.SEND_LOCK_MINUTES + 1)).isoformat()
        fresh = datetime.now(tz=timezone.utc).isoformat()
        for pk, at in zip(self.ids, (old, fresh)):
            item = PaylistItem.objects.get(pk=pk)
            PaylistItem.objects.filter(pk=pk).update(json_ext={
                **item.json_ext, withdrawal.KEY: {'status': withdrawal.SENDING, 'sending_at': at}})
        self.assertEqual(list(withdrawal.pending().values_list('pk', flat=True)), [self.ids[0]])
