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
        result = withdrawal.send(self.user)
        self.assertEqual(result['sent'], 2)
        self.assertEqual(self.producer.publish.call_count, 2)
        self.assertEqual(self.states(), [withdrawal.SENT, withdrawal.SENT])

    def test_a_failed_send_puts_the_payees_back(self):
        self.producer.publish.return_value = {'ok': False, 'status_code': 500, 'error': 'down'}
        self.assertEqual(withdrawal.send(self.user)['failed'], 2)
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


class MuseWithdrawalQueueAndRemindDbTest(MuseWithdrawalLockDbTest):
    def ext(self, pk):
        return (PaylistItem.objects.get(pk=pk).json_ext or {}).get(withdrawal.KEY) or {}

    def test_the_request_claims_and_the_worker_sends(self):
        self.producer.publish.return_value = {'ok': True, 'status_code': 200, 'esb_body': {}}
        with mock.patch('tasaf_payment.tasks.send_unapplied_withdrawals_task.delay') as delay:
            self.assertEqual(withdrawal.queue_send(self.user), {'queued': True, 'count': 2})
        self.assertEqual(self.states(), [withdrawal.SENDING, withdrawal.SENDING])
        self.assertEqual(withdrawal.pending().count(), 0)
        item_ids, previous, user_id, run_id = delay.call_args.args
        self.assertEqual((sorted(item_ids), user_id), (sorted(self.ids), self.user.id))
        self.assertEqual(withdrawal.run_claimed(item_ids, previous, self.user, run_id)['sent'], 2)
        self.assertEqual(self.states(), [withdrawal.SENT, withdrawal.SENT])

    def test_the_worker_skips_payees_another_send_took_over(self):
        with mock.patch('tasaf_payment.tasks.send_unapplied_withdrawals_task.delay') as delay:
            withdrawal.queue_send(self.user)
        item_ids, previous, _, _ = delay.call_args.args
        self.assertEqual(withdrawal.run_claimed(item_ids, previous, self.user, 'other-run')['sent'], 0)
        self.producer.publish.assert_not_called()

    def test_no_broker_sends_inline(self):
        self.producer.publish.return_value = {'ok': True, 'status_code': 200, 'esb_body': {}}
        with mock.patch('tasaf_payment.tasks.send_unapplied_withdrawals_task.delay', side_effect=OSError('no broker')):
            self.assertEqual(withdrawal.queue_send(self.user)['sent'], 2)

    def test_heartbeat_keeps_the_lock(self):
        withdrawal._claim(self.user)
        old = (datetime.now(tz=timezone.utc) - timedelta(minutes=withdrawal.SEND_LOCK_MINUTES + 1)).isoformat()
        for pk in self.ids:
            ext = PaylistItem.objects.get(pk=pk).json_ext
            ext[withdrawal.KEY]['sending_at'] = old
            PaylistItem.objects.filter(pk=pk).update(json_ext=ext)
        self.assertEqual(withdrawal.pending().count(), 2)
        withdrawal._heartbeat(self.ids)
        self.assertEqual(withdrawal.pending().count(), 0)
        self.assertEqual(self.ext(self.ids[0])['status'], withdrawal.SENDING)

    def test_unanswered_payees_are_announced_once_and_a_resend_clears_it(self):
        self.producer.publish.return_value = {'ok': True, 'status_code': 200, 'esb_body': {}}
        withdrawal.send(self.user)
        late = (datetime.now(tz=timezone.utc) - timedelta(minutes=31)).isoformat()
        for pk in self.ids:
            ext = PaylistItem.objects.get(pk=pk).json_ext
            ext[withdrawal.KEY]['sent_at'] = late
            PaylistItem.objects.filter(pk=pk).update(json_ext=ext)
        with mock.patch('tasaf_payment.events.emit_withdrawal_event') as emitted:
            self.assertEqual(len(withdrawal.remind(30, apply=True)), 2)
            self.assertEqual(withdrawal.remind(30, apply=True), [])
        emitted.assert_called_once()
        self.assertEqual(emitted.call_args.kwargs['count'], 2)
        self.assertTrue(self.ext(self.ids[0]).get(withdrawal.REMINDED))
        for pk in self.ids:
            ext = PaylistItem.objects.get(pk=pk).json_ext
            ext[withdrawal.KEY]['status'] = withdrawal.REJECTED
            PaylistItem.objects.filter(pk=pk).update(json_ext=ext)
        withdrawal.send(self.user)
        self.assertIsNone(self.ext(self.ids[0]).get(withdrawal.REMINDED))
