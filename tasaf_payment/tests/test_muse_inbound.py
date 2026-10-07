from django.test import SimpleTestCase

from tasaf_payment import muse_inbound as mi


class MuseInboundPureTest(SimpleTestCase):
    def test_bank_reference_from_status_text(self):
        self.assertEqual(mi.bank_reference(
            'Settled through TIPS on 2026-02-27 with Banking Reference DBR9N68VM0T'), 'DBR9N68VM0T')
        self.assertIsNone(mi.bank_reference('Beneficiary name mismatch'))
        self.assertIsNone(mi.bank_reference(None))

    def test_ack_follows_muses_shape(self):
        ack = mi.ack_message('PAYMENT_STATUS', 'SP84E202745958')['message']
        self.assertEqual({k: ack['messageHeader'][k] for k in ('sender', 'receiver', 'messageType')},
                         {'sender': 'TASAF MIS', 'receiver': 'MUSE', 'messageType': 'ACK'})
        self.assertRegex(ack['messageHeader']['msgId'], r'^TA[0-9A-F]{14}$')
        self.assertEqual(ack['messageSummary'], {'orgMessageType': 'PAYMENT_STATUS', 'orgMsgId': 'SP84E202745958',
                                                 'status': 'RECEIVED', 'statusDesc': 'Received Successfully'})

    def test_both_spellings_of_sent_to_bank(self):
        for raw in ('SENT TO BANK', 'SENT_TO_BANK', 'sent to bank'):
            self.assertEqual(mi.BATCH_STATUSES[raw.strip().upper().replace('_', ' ')], 'SENT_TO_BANK')

    def test_rejected_ack_carries_the_reason(self):
        ack = mi.ack_message('RESPONSE', 'SP1', 'No paylist was sent with msgId', status='REJECTED')['message']
        self.assertEqual(ack['messageSummary'], {'orgMessageType': 'RESPONSE', 'orgMsgId': 'SP1',
                                                 'status': 'REJECTED',
                                                 'statusDesc': 'No paylist was sent with msgId'})


class MuseInboundReplyTest(SimpleTestCase):
    """handle() never answers with an HTTP code: GovESB drops non-2xx bodies."""

    def _message(self, **parts):
        return {'message': {'messageHeader': {'msgId': 'MU1', 'messageType': 'RESPONSE'}, **parts}}

    def test_not_a_muse_message_is_a_failure(self):
        self.assertEqual(mi.handle({'hello': 1}), (False, 'Not a MUSE message: no message.messageHeader'))

    def test_refusal_is_a_rejected_ack(self):
        ok, body = mi.handle(self._message(messageSummary={'status': 'LOST', 'orgMsgId': 'X'}))
        self.assertTrue(ok)
        self.assertEqual(body['message']['messageSummary'],
                         {'orgMessageType': 'RESPONSE', 'orgMsgId': 'MU1', 'status': 'REJECTED',
                          'statusDesc': "Unknown batch status: 'LOST'"})

    def test_message_without_summary_or_details_is_rejected(self):
        ok, body = mi.handle(self._message())
        self.assertTrue(ok)
        self.assertEqual(body['message']['messageSummary']['status'], 'REJECTED')


class MuseMessageViewTest(SimpleTestCase):
    """The endpoint GovESB calls: always HTTP 200 and a reply signed with our key."""

    def setUp(self):
        import base64
        import tempfile
        from pathlib import Path
        from ellipticcurve import PrivateKey
        self.ours, self.esb = PrivateKey(), PrivateKey()
        key_path = Path(tempfile.mkdtemp()) / 'client.pem'
        key_path.write_text(self.ours.toPem())
        self.settings_esb = {'ENABLED': True, 'CLIENT_PRIVATE_KEY': str(key_path),
                             'GOV_ESB_PUBLIC_KEY_B64': base64.b64encode(self.esb.publicKey().toDer()).decode()}

    def _post(self, esb_body, tamper=False):
        import json
        from django.test import RequestFactory, override_settings
        from ellipticcurve import Ecdsa
        from tasaf_payment.views import MuseMessageView
        data_text = json.dumps({'esbBody': esb_body})
        signature = Ecdsa.sign(data_text, self.esb).toBase64()
        if tamper:
            data_text = data_text.replace('MU1', 'MU2')
        raw = '{"data": ' + data_text + ', "signature": "' + signature + '"}'
        request = RequestFactory().post('/api/tasaf_payment/muse/message/', raw, content_type='application/json')
        with override_settings(ESB=self.settings_esb):
            return MuseMessageView.as_view()(request)

    def _verified_data(self, response):
        import json
        from ellipticcurve import Ecdsa
        from ellipticcurve import Signature as EcSignature
        from coremis_app_integration.esb_client.envelope import raw_json_member
        text = response.content.decode()
        self.assertTrue(Ecdsa.verify(raw_json_member(text, 'data'),
                                     EcSignature.fromBase64(json.loads(text)['signature']), self.ours.publicKey()))
        return json.loads(text)['data']

    def test_refusal_comes_back_signed_as_rejected_ack(self):
        response = self._post({'message': {'messageHeader': {'msgId': 'MU1', 'messageType': 'RESPONSE'},
                                           'messageSummary': {'status': 'LOST'}}})
        self.assertEqual(response.status_code, 200)
        data = self._verified_data(response)
        self.assertTrue(data['success'])
        self.assertEqual(data['esbBody']['message']['messageSummary']['status'], 'REJECTED')

    def test_bad_signature_is_http_200_success_false(self):
        response = self._post({'message': {'messageHeader': {'msgId': 'MU1'}}}, tamper=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._verified_data(response), {'success': False, 'message': 'Invalid GovESB signature'})


class FindItemOrgMsgIdTest(SimpleTestCase):
    """MUSE's spec sends orgMsgId as null, our msgId, or the payment's own reference."""

    def _run(self, matches_our_batch):
        from unittest import mock
        items, narrowed = mock.MagicMock(name='items'), mock.MagicMock(name='narrowed')
        narrowed.exists.return_value = matches_our_batch
        items.filter.side_effect = lambda **kw: narrowed if 'paylist__muse_msg_id' in kw else items
        narrowed.filter.side_effect = lambda **kw: narrowed
        with mock.patch('tasaf_payment.models.PaylistItem.objects') as objects:
            objects.filter.return_value.exclude.return_value.select_related.return_value = items
            mi._find_item('P84E20274', 'P84E20274')
        return items, narrowed

    def test_reference_that_is_not_our_msg_id_does_not_narrow(self):
        items, narrowed = self._run(False)
        self.assertTrue(items.order_by.called)
        self.assertFalse(narrowed.order_by.called)

    def test_our_msg_id_narrows(self):
        items, narrowed = self._run(True)
        self.assertTrue(narrowed.order_by.called)
        self.assertFalse(items.order_by.called)
