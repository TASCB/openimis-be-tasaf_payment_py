from datetime import datetime

from django.test import SimpleTestCase

from tasaf_payment.tips import message as tm

SETTINGS = {'sender': 'MOFPTZT0', 'receiver': 'TANZTZT0', 'service_code_single': 'SRVC619734',
            'service_code_bulk': 'SRVC728102', 'summary_identifier': '20610004189', 'summary_fsp_code': '016'}

CALLBACK = {'message': {
    'messageHeader': {'sender': 'TANZTZT0', 'receiver': 'MOFPTZT0', 'messageType': 'TIPSBULKLOOKUP',
                      'msgId': 'TPPS_99127182781275'},
    'messageSummary': {'recordsSuccessful': 4, 'recordsFailed': 2, 'recordsReceived': 6},
    'messageDetails': [
        {'identifierType': 'BANK', 'identifier': '20610004189', 'fspId': '016', 'fullName': 'ELIVIRA PAUL AMMA',
         'accountCategory': 'PERSON', 'accountType': 'BANK', 'lookupId': 'd77167f1'},
        {'lookupId': 'd6b69364'},
        {'fspDestination': '501', 'identifierType': 'MSISDN', 'identifier': '07142140795',
         'errorInformation': {'errorCode': 3100, 'errorDescription': 'MSISDN not valid must have 10 or 12 character'}},
        {'fspDestination': '501', 'identifierType': 'MSISDN', 'identifier': '0742140795',
         'errorInformation': {'errorCode': '3204', 'errorDescription': 'Error from Payee FSP - Party not found'}},
    ]}}


class TipsMessageTest(SimpleTestCase):
    def test_msg_id_starts_with_the_tasaf_prefix(self):
        msg_id = tm.new_msg_id(datetime(2026, 11, 20, 14, 30, 5))
        self.assertTrue(msg_id.startswith('TASAF_20112026143005'))
        self.assertEqual(len(msg_id), len('TASAF_20112026143005') + 4)

    def test_single_matches_the_shared_sample(self):
        body = tm.build_single(SETTINGS, 'TASAF_1', 'BANK', '20610004189', '016')
        self.assertEqual(body['message']['messageHeader'],
                         {'sender': 'MOFPTZT0', 'receiver': 'TANZTZT0', 'messageType': 'TIPSBULKLOOKUP',
                          'msgId': 'TASAF_1'})
        self.assertEqual(body['message']['messageSummary']['fsp-destination'], '016')
        self.assertIn('digitalSignature', body)
        self.assertEqual(tm.check(body, tm.SINGLE), [])
        self.assertEqual(tm.headers(SETTINGS, tm.SINGLE), {'service-code': 'SRVC619734'})

    def test_bulk_lists_each_account_and_signs_inside_message(self):
        items = [('BANK', '00101044688401', '067'), ('MSISDN', '255742140795', '501')]
        body = tm.build_bulk(SETTINGS, 'TASAF_2', items)
        self.assertEqual(len(body['message']['messageDetails']), 2)
        self.assertIn('digitalSignature', body['message'])
        self.assertEqual(tm.check(body, tm.BULK), [])
        self.assertEqual(tm.headers(SETTINGS, tm.BULK), {'service-code': 'SRVC728102'})

    def test_check_finds_bad_numbers_types_and_duplicates(self):
        body = tm.build_bulk({}, 'X1', [('MSISDN', '07142140795', '501'), ('CONTROLNO', '991', '01'),
                                        ('BANK', '123', '016'), ('BANK', '123', '016')])
        found = {(p['field'], p['index']) for p in tm.check(body, tm.BULK)}
        self.assertIn(('sender', None), found)
        self.assertIn(('msgId', None), found)
        self.assertIn(('messageDetails.identifier', 0), found)
        self.assertIn(('messageDetails.identifierType', 1), found)
        self.assertIn(('messageDetails.fspDestination', 1), found)
        self.assertIn(('messageDetails.identifier', 3), found)

    def test_callback_entries_become_outcomes(self):
        results = tm.results_in(CALLBACK)
        self.assertEqual([r['outcome'] for r in results], [tm.FOUND, tm.EMPTY, tm.INVALID, tm.NOT_FOUND])
        self.assertEqual(results[0]['full_name'], 'ELIVIRA PAUL AMMA')
        self.assertEqual(results[2]['error_code'], '3100')


class TipsSingleReplyTest(SimpleTestCase):
    def test_a_single_reply_from_tips_is_read(self):
        reply = {'digitalSignature': 'x', 'message': {
            'messageHeader': {'receiver': 'MOFPTZT0', 'messageType': 'TIPSLOOKUP', 'sender': 'TANZTZT0',
                              'msgId': 'FB20261121104631034647'},
            'messageSummary': {'identifier': '0011055818', 'payloadType': 'JSON', 'accountCategory': 'PERSON',
                               'identity': {'type': '', 'value': ''}, 'accountType': 'BANK',
                               'fullName': 'TEST  HOLDER', 'fspDestination': '013', 'identifierType': 'BANK',
                               'fspId': '013'}}}
        reply['message']['messageSummary']['identity'] = {'type': 'NIN', 'value': '1'}
        self.assertNotIn('identity', tm.without_identity(reply)['message']['messageSummary'])
        [result] = tm.results_in(reply)
        self.assertEqual((result['outcome'], result['identifier'], result['fsp_code'], result['full_name']),
                         (tm.FOUND, '0011055818', '013', 'TEST  HOLDER'))

    def test_a_reply_without_a_name_or_error_is_an_empty_answer(self):
        reply = {'message': {'messageHeader': {'messageType': 'TIPSLOOKUP', 'msgId': 'FB1'},
                             'messageSummary': {'identifier': '9120000178013', 'payloadType': 'JSON',
                                                'fspDestination': '006', 'identifierType': 'BANK'}}}
        [result] = tm.results_in(reply)
        self.assertEqual((result['outcome'], result['fsp_code']), (tm.EMPTY, '006'))
        self.assertEqual(tm.results_in({'message': {'messageHeader': {'messageType': 'ACK'},
                                                    'messageSummary': {'identifier': '1'}}}), [])

    def test_a_bulk_ack_carries_no_results(self):
        ack = {'message': {'messageSummary': {'dateTime': '2026-11-20 17:27:26', 'response': 'ACCEPTED BY TIPS',
                                              'request-id': 'TASAF_1', 'responseCode': 202, 'status': 'RECEIVED'},
                           'messageHeader': {'receiver': 'MOFPTZT0', 'messageType': 'ACK', 'sender': 'TANZTZT0',
                                             'msgId': 'FB1'},
                           'digitalSignature': 'x'}}
        self.assertEqual(tm.results_in(ack), [])

    def test_results_nested_under_response_are_read(self):
        callback = {'message': {'messageHeader': {'messageType': 'TIPSBULKLOOKUP', 'msgId': 'FB1'},
                                'messageDetails': [
            {'fspDestination': '067', 'identifierType': 'BANK', 'identifier': '00101044688401',
             'response': {'identifierType': 'BANK', 'identifier': '00101044688401', 'fspId': '067',
                          'fullName': 'Example Recipient', 'accountCategory': 'PERSON'}},
            {'fspDestination': '501', 'identifierType': 'MSISDN', 'identifier': '255700000000',
             'response': {'errorInformation': {'errorCode': '3204', 'errorDescription': 'Party not found'}}}]}}
        found, missing = tm.results_in(callback)
        self.assertEqual((found['outcome'], found['full_name'], found['fsp_code']),
                         (tm.FOUND, 'Example Recipient', '067'))
        self.assertEqual((missing['outcome'], missing['error_code'], missing['fsp_code']),
                         (tm.NOT_FOUND, '3204', '501'))
