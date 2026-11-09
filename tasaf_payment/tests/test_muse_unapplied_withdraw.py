from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment.models import UNAPPLIED_ACTION_KEY
from tasaf_payment.muse import withdrawal


def item(decision, end_to_end_id='1FJ8SKD9R1', amount='20000'):
    return SimpleNamespace(json_ext={UNAPPLIED_ACTION_KEY: decision}, end_to_end_id=end_to_end_id,
                           benefit_consumption=None, amount=amount)


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
        with mock.patch.object(withdrawal, '_claim', return_value=(rows, {})), \
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
