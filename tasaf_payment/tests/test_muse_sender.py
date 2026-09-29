from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from coremis_app_integration.esb_client.exceptions import (
    ESBConfigurationError, ESBRequestError, ESBSignatureError,
)
from tasaf_payment import muse_sender as ms

BODY = {'message': {'messageHeader': {'msgId': 'TMA0F744FC42A601', 'createdAt': '2026-09-28 10:00:00'},
                    'paymentSummary': {'referenceNo': 'TP260904-A0F744FC', 'noOfTransaction': 2,
                                       'totalAmount': 79}}}
OK = {'published': True, 'ok': True, 'status_code': 200, 'request_id': 'R1', 'esb_body': {}}


class MuseSenderTest(SimpleTestCase):
    def setUp(self):
        self.paylist = SimpleNamespace(uuid='u', muse_msg_id=None, json_ext={})
        self.logs = []
        patches = [
            mock.patch.object(ms.muse_message, 'build', return_value=BODY),
            mock.patch.object(ms.muse_message, 'check', return_value=[]),
            mock.patch.object(ms, '_freeze_ids'),
            mock.patch.object(ms, '_remember'),
            mock.patch.object(ms, '_log', side_effect=lambda *a, **k: self.logs.append([a[3]]) or self.logs[-1]),
            mock.patch.object(ms, '_close_log', side_effect=lambda entry, status, **k: entry.append(status)),
            mock.patch('coremis_app_integration.govesb.govesb_enabled', return_value=True),
            mock.patch.object(ms, '_config', side_effect=lambda name, default: default),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sleeps = []

    def send(self, *responses):
        producer = mock.Mock()
        producer.publish.side_effect = list(responses)
        with mock.patch('coremis_app_integration.govesb.GovESBProducer', return_value=producer):
            result = ms.MuseSender(SimpleNamespace(username='maker'), sleep=self.sleeps.append).send(
                self.paylist, 'tasaf.payment.submit')
        return result, producer

    def test_sent_first_time(self):
        result, producer = self.send(OK)
        self.assertEqual((result.outcome, result.submitted, result.attempts), (ms.SENT, True, 1))
        self.assertEqual(producer.publish.call_args[0][1], BODY)
        self.assertEqual(self.logs, [['PENDING', 'SUCCESS']])

    def test_transport_error_is_retried_then_sent(self):
        result, _ = self.send(ESBRequestError('timeout'), OK)
        self.assertEqual((result.outcome, result.attempts), (ms.SENT, 2))
        self.assertEqual(self.logs, [['PENDING', 'RETRYING'], ['PENDING', 'SUCCESS']])
        self.assertEqual(self.sleeps, [2.0])

    def test_server_error_retried_until_exhausted(self):
        busy = {'published': True, 'ok': False, 'status_code': 503, 'error': 'busy', 'esb_body': None}
        result, producer = self.send(busy, busy, busy)
        self.assertEqual((result.outcome, result.submitted, producer.publish.call_count), (ms.FAILED, False, 3))
        self.assertEqual([log[-1] for log in self.logs], ['RETRYING', 'RETRYING', 'FAILED'])
        self.assertEqual(self.sleeps, [2.0, 4.0])

    def test_client_error_not_retried(self):
        result, producer = self.send({'published': True, 'ok': False, 'status_code': 400, 'error': 'bad', 'esb_body': None})
        self.assertEqual((result.outcome, producer.publish.call_count), (ms.FAILED, 1))
        self.assertEqual(self.logs, [['PENDING', 'REJECTED']])

    def test_unverifiable_answer_is_unknown(self):
        result, producer = self.send(ESBSignatureError('bad signature'))
        self.assertEqual((result.outcome, result.submitted, producer.publish.call_count), (ms.UNKNOWN, False, 1))

    def test_misconfiguration_not_retried(self):
        result, producer = self.send(ESBConfigurationError('no key'))
        self.assertEqual((result.outcome, producer.publish.call_count), (ms.FAILED, 1))

    def test_refuses_what_muse_would_reject(self):
        problem = {'section': 'payList', 'field': 'payeeCode', 'rule': 'pattern', 'message': 'too long'}
        with mock.patch.object(ms.muse_message, 'check', return_value=[problem]), \
                mock.patch.object(ms.muse_message, 'summarise',
                                  return_value=[{**problem, 'count': 710}]):
            result, producer = self.send(OK)
        self.assertEqual((result.outcome, result.submitted), (ms.REFUSED, False))
        self.assertIn('payList.payeeCode', result.message)
        producer.publish.assert_not_called()

    def test_refuses_when_govesb_off(self):
        with mock.patch('coremis_app_integration.govesb.govesb_enabled', return_value=False):
            result, producer = self.send(OK)
        self.assertEqual(result.outcome, ms.REFUSED)
        producer.publish.assert_not_called()

    def test_record_only_mode_marks_without_sending(self):
        with mock.patch.object(ms, '_config', side_effect=lambda n, d: 'RECORD_ONLY' if n == 'muse_submit_mode' else d):
            result, producer = self.send(OK)
        self.assertEqual((result.outcome, result.submitted), (ms.RECORDED, True))
        self.assertEqual(self.logs, [['NOT_SENT']])
        producer.publish.assert_not_called()
