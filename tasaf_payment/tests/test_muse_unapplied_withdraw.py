from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment.models import UNAPPLIED_ACTION_KEY
from tasaf_payment.muse import withdrawal


def item(decision, end_to_end_id='1FJ8SKD9R1', amount='20000', id=None):
    return SimpleNamespace(json_ext={UNAPPLIED_ACTION_KEY: decision}, end_to_end_id=end_to_end_id,
                           benefit_consumption=None, amount=amount, id=id)


class WithdrawMessageTest(SimpleTestCase):
    def build(self, decision, **kw):
        with mock.patch.object(withdrawal, '_full_name', side_effect=lambda login: f'Name of {login}'):
            return withdrawal.build(item(decision, **kw), 'TW0123456789ABCD')

    def test_shape_matches_muse_sample(self):
        body = self.build({'action': 'WITHDRAW', 'by': 'pct1', 'note': 'Payee moved'})
        self.assertEqual(set(body), {'message', 'digitalSignature'})
        header = body['message']['messageHeader']
        self.assertEqual((header['messageType'], header['msgId'], header['receiver']),
                         ('UNAPPLIED_WITHDRAW', 'TW0123456789ABCD', 'MUSE'))
        self.assertEqual(body['message']['messageDetails'], {
            'paymentReference': '1FJ8SKD9R1', 'withdrawReason': 'Payee moved', 'withdrawnBy': 'Name of pct1'})

    def test_provider_change_names_who_changed_it(self):
        details = self.build({'action': 'WITHDRAW', 'by': 'pct1', 'auto': 'PROVIDER_CHANGED',
                              'changed_by': 'paaf2', 'note': 'old note'})['message']['messageDetails']
        self.assertEqual((details['withdrawReason'], details['withdrawnBy']), ('Provider changed', 'Name of paaf2'))

    def test_reason_is_trimmed(self):
        details = self.build({'action': 'WITHDRAW', 'by': 'pct1', 'note': 'x' * 400})['message']['messageDetails']
        self.assertEqual(len(details['withdrawReason']), withdrawal.REASON_MAX)

    def test_reference_falls_back_to_the_benefit_code(self):
        benefit = SimpleNamespace(code='1FJ8SKD9')
        row = item({'action': 'WITHDRAW', 'by': 'pct1'}, end_to_end_id=None)
        row.benefit_consumption = benefit
        self.assertEqual(withdrawal.payment_reference(row), '1FJ8SKD9')


class WithdrawSendTest(SimpleTestCase):
    def run_send(self, rows, outcomes):
        producer = mock.Mock()
        producer.publish.side_effect = outcomes
        released = []
        self.announced = mock.Mock()
        with mock.patch.object(withdrawal, '_claim', return_value=(rows, {})), \
                mock.patch.object(withdrawal, '_announce_run', self.announced), \
                mock.patch.object(withdrawal, '_producer', return_value=producer), \
                mock.patch.object(withdrawal, '_release', side_effect=lambda items, u, p: released.extend(items)), \
                mock.patch.object(withdrawal, '_stamp'), mock.patch.object(withdrawal, '_log'), \
                mock.patch.object(withdrawal, '_full_name', return_value='A B'), \
                mock.patch('tasaf_payment.muse.sender.muse_config', return_value='SEND'), \
                mock.patch('tasaf_payment.muse.sender.muse_reply', return_value=None):
            return withdrawal.send(mock.Mock(username='pct1')), producer, released

    def test_one_message_per_payment_and_a_failure_puts_back_only_that_one(self):
        rows = [item({'action': 'WITHDRAW', 'by': 'pct1'}, end_to_end_id=f'CODE{i}') for i in range(3)]
        ok = {'ok': True, 'status_code': 200}
        result, producer, released = self.run_send(rows, [ok, {'ok': False, 'error': 'busy'}, ok])
        self.assertEqual(producer.publish.call_count, 3)
        refs = [c.args[1]['message']['messageDetails']['paymentReference'] for c in producer.publish.call_args_list]
        self.assertEqual(refs, ['CODE0', 'CODE1', 'CODE2'])
        self.assertEqual((result['sent'], result['failed']), (2, 1))
        self.assertEqual(released, [rows[1]])

    def test_stops_after_repeated_failures_and_puts_the_rest_back(self):
        rows = [item({'action': 'WITHDRAW', 'by': 'pct1'}, end_to_end_id=f'CODE{i}') for i in range(6)]
        result, producer, released = self.run_send(rows, RuntimeError('timeout'))
        self.assertEqual(producer.publish.call_count, withdrawal.STOP_AFTER_FAILURES)
        self.assertEqual((result['sent'], result['failed']), (0, 6))
        self.assertEqual(released, rows)

    def test_payment_without_reference_is_skipped(self):
        rows = [item({'action': 'WITHDRAW', 'by': 'pct1'}, end_to_end_id=None)]
        result, producer, released = self.run_send(rows, [])
        self.assertEqual((result['skipped'], producer.publish.call_count, released), (1, 0, rows))

    def test_one_notice_per_send_names_only_the_payments_that_went(self):
        rows = [item({'action': 'WITHDRAW', 'by': 'pct1'}, end_to_end_id=f'CODE{i}', id=i + 1) for i in range(3)]
        ok = {'ok': True, 'status_code': 200}
        result, _, _ = self.run_send(rows, [ok, {'ok': False, 'error': 'busy'}, ok])
        self.announced.assert_called_once()
        sent_ids, _, summary = self.announced.call_args.args
        self.assertEqual((sent_ids, summary['failed'], summary['errors']), ([1, 3], 1, ['busy']))
        self.assertNotIn('sent_ids', result)


class WithdrawAnswerTest(SimpleTestCase):
    def acknowledge(self, in_send, answered=None, status='REJECTED'):
        row = item({'action': 'WITHDRAW', 'by': 'pct1'})
        if answered:
            row.json_ext[withdrawal.KEY] = {'status': answered}
        emitted, self.stamped = mock.Mock(), mock.Mock()
        token = withdrawal._IN_SEND.set(in_send)
        try:
            with mock.patch.object(withdrawal.PaylistItem.objects, 'filter', return_value=[row]), \
                    mock.patch.object(withdrawal, '_stamp', self.stamped), \
                    mock.patch('tasaf_payment.services.inbound_audit_user', return_value=None), \
                    mock.patch('django.db.transaction.atomic'), \
                    mock.patch('tasaf_payment.events.emit_withdrawal_event', emitted):
                withdrawal.acknowledge('TW1', status, 'not configured')
        finally:
            withdrawal._IN_SEND.reset(token)
        return emitted

    def test_a_late_answer_is_announced(self):
        emitted = self.acknowledge(in_send=False)
        emitted.assert_called_once()
        self.assertEqual(emitted.call_args.kwargs['status'], withdrawal.REJECTED)
        self.assertEqual(emitted.call_args.kwargs['references'], ['1FJ8SKD9R1'])

    def test_an_answer_inside_the_send_waits_for_the_send_notice(self):
        self.acknowledge(in_send=True).assert_not_called()

    def test_a_repeated_answer_is_not_announced_again(self):
        self.acknowledge(in_send=False, answered=withdrawal.RECEIVED, status='RECEIVED').assert_not_called()
        self.stamped.assert_not_called()

    def test_a_refusal_after_received_is_ignored(self):
        self.acknowledge(in_send=False, answered=withdrawal.RECEIVED, status='REJECTED').assert_not_called()
        self.stamped.assert_not_called()
