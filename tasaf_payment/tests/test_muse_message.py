import uuid
from datetime import datetime
from types import SimpleNamespace

from django.test import SimpleTestCase

from tasaf_payment import muse_message as mm

NOW = datetime(2026, 1, 8, 18, 18, 6)
SETTINGS = {'institutionCode': '000M0048', 'payerAccount': '9921473423', 'subBudgetClass': 202,
            'paymentDesc': 'Malipo kwa walengwa', 'currencyCode': 'TZS', 'isStp': 'False',
            'gl_accounts': []}
IDS = {'msgId': 'TMA0F744FC42A601', 'referenceNo': 'TP260904-A0F744FC'}


def payee(**overrides):
    row = {'payeeCode': 'PA16E50291', 'payeeName': 'Issa Hashimu Milanzi',
           'payeeAccountNumber': '712105032650', 'payeeAccountName': 'Issa Hashimu Milanzi',
           'payeeBankName': 'NMB', 'payeeBankBic': 'NMIBTZTZ', 'amount': 29,
           'endToEndId': 'PA16E50291', 'paymentChannel': 'BANK'}
    row.update(overrides)
    return row


def fields(problems):
    return {(p['section'], p['field'], p['rule']) for p in problems}


class MuseMessageBuildTest(SimpleTestCase):
    def test_valid_message_has_no_problems(self):
        body = mm.assemble(IDS, SETTINGS, [payee(), payee(endToEndId='PA16E50292', amount=50)], NOW)
        self.assertEqual(mm.check(body), [])
        summary = body['message']['paymentSummary']
        self.assertEqual(summary['totalAmount'], 79)
        self.assertEqual(summary['noOfTransaction'], 2)
        self.assertEqual(body['message']['messageHeader']['createdAt'], '2026-01-08 18:18:06')
        self.assertNotIn('glList', body['message'])

    def test_ids_are_deterministic_and_fit_the_rules(self):
        paylist = SimpleNamespace(uuid=uuid.UUID('a0f744fc-42a6-4765-a505-24c14f39d353'),
                                  json_ext={}, date_created=datetime(2026, 9, 4))
        self.assertEqual(mm.msg_id(paylist), 'TMA0F744FC42A601')
        self.assertEqual(mm.reference_no(paylist), 'TP260904-A0F744FC')
        paylist.json_ext = {'muse_attempt': 3}
        self.assertEqual(mm.msg_id(paylist), 'TMA0F744FC42A603')

    def test_money(self):
        self.assertEqual(mm.money('29000.00'), 29000)
        self.assertEqual(mm.money('15.5'), 15.5)
        self.assertEqual(mm.money(None), 0)

    def test_financial_year_ends_in_june(self):
        self.assertEqual(mm.financial_year(datetime(2026, 1, 8)), '2026')
        self.assertEqual(mm.financial_year(datetime(2026, 7, 1)), '2027')

    def test_one_gl_line_takes_the_total(self):
        settings = {**SETTINGS, 'gl_accounts': [{'glaccount': '052|0048', 'grantName': 'TZAM'}]}
        body = mm.assemble(IDS, settings, [payee()], NOW)
        line = body['message']['glList'][0]
        self.assertEqual((line['amount'], line['financialYear'], line['reference']),
                         (29, '2026', IDS['referenceNo']))
        self.assertEqual(mm.check(body), [])

    def test_several_gl_lines_are_reported_not_guessed(self):
        settings = {**SETTINGS, 'gl_accounts': [{'glaccount': 'A'}, {'glaccount': 'B'}]}
        body = mm.assemble(IDS, settings, [payee()], NOW)
        self.assertIn(('glList', 'amount', 'undecided'), fields(mm.check(body)))


class MuseMessageCheckTest(SimpleTestCase):
    def test_hhid_payee_code_and_uuid_end_to_end_id_fail(self):
        body = mm.assemble(IDS, SETTINGS, [payee(payeeCode='P3-020109102-33067946',
                                                  endToEndId='DEMO-FDE4BD03AF')], NOW)
        problems = mm.check(body)
        self.assertEqual(fields(problems), {('payList', 'payeeCode', 'pattern'),
                                            ('payList', 'endToEndId', 'pattern')})
        self.assertEqual(problems[0]['index'], 0)

    def test_missing_settings_and_bic(self):
        empty = mm.settings_values(None)
        body = mm.assemble(IDS, empty, [payee(payeeBankBic='', payeeBankName='')], NOW)
        found = fields(mm.check(body))
        for field in ('institutionCode', 'payerAccount', 'subBudgetClass', 'paymentDesc'):
            self.assertIn(('paymentSummary', field, 'required'), found)
        self.assertIn(('payList', 'payeeBankBic', 'required'), found)

    def test_bad_values(self):
        body = mm.assemble(IDS, SETTINGS, [
            payee(payeeBankBic='NMIBKEKX', payeeName='Zaïtuni', amount=0,
                  payeeAccountNumber='123', paymentChannel='CASH'),
        ], NOW)
        found = fields(mm.check(body))
        self.assertTrue({('payList', 'payeeBankBic', 'pattern'), ('payList', 'payeeName', 'pattern'),
                         ('payList', 'amount', 'range'), ('payList', 'payeeAccountNumber', 'pattern'),
                         ('payList', 'paymentChannel', 'choice')} <= found)

    def test_duplicate_end_to_end_id_and_totals(self):
        body = mm.assemble(IDS, SETTINGS, [payee(), payee()], NOW)
        body['message']['paymentSummary']['totalAmount'] = 1
        body['message']['paymentSummary']['noOfTransaction'] = 5
        found = fields(mm.check(body))
        self.assertIn(('payList', 'endToEndId', 'duplicate'), found)
        self.assertIn(('paymentSummary', 'totalAmount', 'mismatch'), found)
        self.assertIn(('paymentSummary', 'noOfTransaction', 'mismatch'), found)

    def test_empty_paylist(self):
        body = mm.assemble(IDS, SETTINGS, [], NOW)
        self.assertIn(('payList', 'payList', 'required'), fields(mm.check(body)))

    def test_summarise_groups_identical_failures(self):
        rows = [payee(payeeCode='P3-020109102-3306794%d' % i, endToEndId='E%d' % i) for i in range(5)]
        summary = mm.summarise(mm.check(mm.assemble(IDS, SETTINGS, rows, NOW)))
        self.assertEqual(summary, [{'section': 'payList', 'field': 'payeeCode', 'rule': 'pattern',
                                    'message': 'letters and digits only, at most 15', 'count': 5}])
