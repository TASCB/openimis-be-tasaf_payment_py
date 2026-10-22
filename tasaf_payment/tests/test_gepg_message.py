from types import SimpleNamespace

from django.test import SimpleTestCase

from tasaf_payment.gepg import message as gm

SETTINGS = {'sp_code': 'SP991', 'sp_sys_id': 'TASAF001'}


def body(items=(('1', '255767482671'),), bic='AMTLTZTX'):
    request = SimpleNamespace(acc_verif_id='TV20261019120000ABCDEF12', bic=bic)
    return gm.build(request, SETTINGS, [SimpleNamespace(item_id=i, acc_num=n) for i, n in items])


class GepgMessageTest(SimpleTestCase):
    def test_build_keeps_the_spec_tag_order(self):
        req = body()[gm.REQUEST]
        self.assertEqual(tuple(req), gm.REQUEST_ORDER)
        self.assertEqual(tuple(req['AccVerifItems'][0]), gm.ITEM_ORDER)
        self.assertEqual(gm.check(body(), 'MOBILE'), [])

    def test_check_finds_bad_mobile_numbers_and_duplicate_item_ids(self):
        problems = gm.check(body(items=(('1', '0767482671'), ('1', '255767482672'))), 'MOBILE')
        fields = {p['field'] for p in problems}
        self.assertEqual(fields, {'AccNum', 'AccVerifItemId'})

    def test_check_requires_settings_and_bic(self):
        b = body(bic='')
        b[gm.REQUEST]['SpCode'] = ''
        fields = {p['field'] for p in gm.check(b, 'MOBILE')}
        self.assertTrue({'SpCode', 'Bic'} <= fields)

    def test_check_refuses_an_empty_request(self):
        self.assertIn('AccVerifItems', {p['field'] for p in gm.check(body(items=()), 'BANK')})

    def test_bank_numbers_are_letters_and_digits(self):
        self.assertEqual(gm.check(body(items=(('1', '20110012345'),), bic='NMIBTZTZ'), 'BANK'), [])
        self.assertTrue(gm.check(body(items=(('1', '2011 0012'),), bic='NMIBTZTZ'), 'BANK'))

    def test_xml_rendering_escapes_and_keeps_order(self):
        b = body()
        b[gm.REQUEST]['SpSysId'] = 'A&B'
        xml = gm.as_xml(b)
        self.assertTrue(xml.startswith('<Gepg><gepgAccVerifSubReq><SpCode>SP991</SpCode><SpSysId>A&amp;B'))
        self.assertIn('<AccVerifItems><AccVerifItem><AccVerifItemId>1</AccVerifItemId>', xml)

    def test_xml_parses_back_to_the_same_body(self):
        b = body(items=(('1', '255767482671'), ('2', '255767482672')))
        self.assertEqual(gm.load(gm.as_xml(b)), {'Gepg': b})
        one = gm.load('<Gepg><gepgAccVerifSubResp><AccVerifId>X</AccVerifId><AccVerifItems><AccVerifItem>'
                      '<AccVerifItemId>1</AccVerifItemId></AccVerifItem></AccVerifItems></gepgAccVerifSubResp></Gepg>')
        self.assertEqual(gm.items_of(gm.find(one, gm.RESPONSE)), [{'AccVerifItemId': '1'}])
        self.assertIsNone(gm.load('<not xml'))

    def test_a_json_ack_inside_an_xml_envelope_is_read(self):
        text = ('{"Gepg":{"gepgAccVerifSubReqAck":{"AccVerifId":"TVTEST0002","AccVerifStsCode":"7322",'
                '"AccVerifStsDesc":"Inactive communication protocol"},"gepgSignature":"EKTu"}}')
        ack = gm.find(text, gm.REQUEST_ACK)
        self.assertEqual((ack['AccVerifId'], ack['AccVerifStsCode']), ('TVTEST0002', '7322'))
        self.assertIsNone(gm.load('{not json'))

    def test_format_switch(self):
        from unittest import mock
        b = body()
        self.assertEqual(gm.serialize(b), gm.as_xml(b))
        with mock.patch('tasaf_payment.apps.TasafPaymentConfig.gepg_message_format', 'json'):
            self.assertEqual(gm.serialize(b), b)

    def test_find_accepts_the_gepg_wrapper(self):
        inner = {'AccVerifId': 'X'}
        self.assertEqual(gm.find({gm.RESPONSE: inner}, gm.RESPONSE), inner)
        self.assertEqual(gm.find({'Gepg': {gm.RESPONSE: inner}}, gm.RESPONSE), inner)
        self.assertIsNone(gm.find({'other': 1}, gm.RESPONSE))

    def test_items_of_accepts_list_wrapped_and_single_forms(self):
        one = {'AccVerifItemId': '1'}
        self.assertEqual(gm.items_of({'AccVerifItems': [one]}), [one])
        self.assertEqual(gm.items_of({'AccVerifItems': {'AccVerifItem': [one]}}), [one])
        self.assertEqual(gm.items_of({'AccVerifItems': {'AccVerifItem': one}}), [one])

    def test_new_ids_fit_and_differ(self):
        a, b = gm.new_acc_verif_id(), gm.new_acc_verif_id()
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 50)

    def test_response_ack(self):
        self.assertEqual(gm.response_ack('X')[gm.RESPONSE_ACK]['AccVerifStsCode'], '7101')
