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
