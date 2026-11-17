from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment import breakdown

PCT_UUID = '7ca95e43-e4bc-4d2f-bf99-804bc57d6c4f'


def pct(base=14000, health=0, children=0, raw=None, capped=None):
    raw = raw if raw is not None else base + health
    return {'calculation_rule_uuid': PCT_UUID, 'pct_breakdown': {
        'base_amount': base, 'young_child_amount': health, 'young_child_count': children,
        'raw_total': raw, 'capped_total': capped if capped is not None else raw}}


class Rows(list):
    def iterator(self, chunk_size=None):
        return iter(self)


class BatchBreakdownTest(SimpleTestCase):
    def report(self, rows, rules=None):
        from calcrule_pct_payment.calculation_rule import PctPaymentCalculationRule
        items = mock.Mock()
        items.values_list.return_value = Rows(rows)
        paylist = SimpleNamespace(muse_batch_reference='TP1', uuid='u', payroll=None)
        with mock.patch.object(breakdown, 'payable_items', return_value=items), \
                mock.patch.object(breakdown, '_rules', return_value=rules or {PCT_UUID: PctPaymentCalculationRule}):
            return breakdown.batch_breakdown(paylist)

    def test_lines_end_on_the_batch_total(self):
        report = self.report([
            # amount, net, charge, benefit amount, benefit json_ext
            (Decimal('22000'), Decimal('21000'), Decimal('1000'), Decimal('21000'), pct(health=7000, children=1)),
            (Decimal('50000'), Decimal('50000'), Decimal('0'), Decimal('50000'),
             pct(health=42000, children=6, raw=56000, capped=50000)),
        ])
        lines = {line['code']: line for line in report['lines']}
        self.assertEqual((lines['DIRECT_SUPPORT']['amount'], lines['DIRECT_SUPPORT']['households']), (28000, 2))
        self.assertEqual((lines['HEALTH']['amount'], lines['HEALTH']['people']), (49000, 7))
        self.assertEqual(report['cap_reduction'], {'amount': 6000, 'households': 1})
        self.assertEqual((report['benefits_total'], report['charges']['amount']), (71000, 1000))
        self.assertEqual(report['batch_total'], 72000)
        self.assertEqual(report['unexplained'], {'amount': 0, 'payments': 0})
        self.assertFalse(report['blocked'])
        csv_total = sum(row[3] for row in breakdown.csv_rows(report)
                        if row[0] in ('Components total', 'Household cap reduction', 'Withdrawal charges',
                                      'Unexplained difference'))
        self.assertEqual(csv_total, report['batch_total'])

    def test_an_edited_amount_blocks_and_is_shown(self):
        report = self.report([(Decimal('15000'), Decimal('15000'), Decimal('0'), Decimal('15000'), pct())])
        self.assertTrue(report['blocked'])
        self.assertEqual(report['unexplained'], {'amount': 1000, 'payments': 1})
        self.assertIn('TZS 1,000', breakdown.refusal(report))

    def test_a_pct_benefit_without_breakdown_blocks(self):
        report = self.report([(Decimal('14000'), None, None, Decimal('14000'),
                               {'calculation_rule_uuid': PCT_UUID})])
        self.assertTrue(report['blocked'])
        self.assertEqual(report['lines'][0]['code'], 'NO_BREAKDOWN')
        self.assertEqual(report['unexplained']['amount'], 0)

    def test_a_missing_breakdown_passes_when_not_required(self):
        with mock.patch('tasaf_payment.apps.TasafPaymentConfig.breakdown_required', False):
            report = self.report([(Decimal('14000'), None, None, Decimal('14000'),
                                   {'calculation_rule_uuid': PCT_UUID})])
        self.assertFalse(report['blocked'])
        self.assertEqual(report['lines'][0]['code'], 'NO_BREAKDOWN')

    def test_a_rule_without_categories_is_one_benefit_line(self):
        report = self.report([(Decimal('30000'), None, None, Decimal('30000'), {'calculation_rule_uuid': 'cash'})],
                             rules={'cash': SimpleNamespace()})
        self.assertFalse(report['blocked'])
        self.assertEqual([(line['code'], line['amount']) for line in report['lines']], [('BENEFIT', 30000)])
        self.assertEqual(report['batch_total'], 30000)


class SubmitBlockTest(SimpleTestCase):
    def test_submit_is_refused_before_it_is_queued(self):
        from tasaf_payment import services
        blocked = {'blocked': True, 'problems': ['1 payment(s) do not match their calculated benefit']}
        query = mock.Mock()
        query.filter.return_value.select_related.return_value.first.return_value = SimpleNamespace()
        with mock.patch.object(services.Paylist, 'objects', query), \
                mock.patch('tasaf_payment.breakdown.batch_breakdown', return_value=blocked), \
                mock.patch.object(services, '_claim_submit') as claim:
            result = services.PaylistService(mock.Mock(is_authenticated=True)).submit('p-1')
        claim.assert_not_called()
        self.assertEqual(result['outcome'], 'REFUSED')
        self.assertIn('does not add up', result['error'])
