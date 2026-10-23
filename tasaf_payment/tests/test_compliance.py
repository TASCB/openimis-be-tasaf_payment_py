from decimal import Decimal

from django.test import SimpleTestCase

from tasaf_payment import compliance as c

PARAMS = {
    'base_amount': Decimal('14000'), 'disability_top_up': Decimal('5000'),
    'young_child_unit': Decimal('7000'), 'young_child_cap_count': 3,
    'primary_unit': Decimal('5000'), 'primary_cap_count': 3,
    'secondary_unit': Decimal('8000'), 'secondary_cap_count': 3,
    'household_cap_amount': Decimal('50000'),
}


def breakdown(**kw):
    out = {'base_amount': 14000, 'has_disability': False, 'disability_amount': 0,
           'young_child_count_raw': 0, 'young_child_count': 0, 'young_child_amount': 0,
           'primary_count_raw': 0, 'primary_count': 0, 'primary_amount': 0,
           'secondary_count_raw': 0, 'secondary_count': 0, 'secondary_amount': 0}
    out.update(kw)
    return out


class ComplianceAssessTest(SimpleTestCase):
    def test_disability_is_once_per_household_and_children_are_capped(self):
        b = breakdown(has_disability=True, disability_amount=5000, young_child_count_raw=4, young_child_count=3,
                      young_child_amount=21000)
        result = c.assess(b, Decimal('40000'), PARAMS)
        self.assertEqual(result['status'], c.MATCH)
        self.assertEqual(result['expected']['parts']['health'], Decimal('21000'))

    def test_household_cap_applies(self):
        b = breakdown(has_disability=True, disability_amount=5000, young_child_count_raw=3, young_child_amount=21000,
                      primary_count_raw=3, primary_amount=15000, secondary_count_raw=3, secondary_amount=24000)
        result = c.assess(b, Decimal('50000'), PARAMS)
        self.assertEqual((result['status'], result['expected']['cap_cut']), (c.MATCH, Decimal('29000')))

    def test_over_and_under_paid_name_the_component(self):
        b = breakdown(young_child_count_raw=1, young_child_amount=14000)
        over = c.assess(b, Decimal('28000'), PARAMS)
        self.assertEqual((over['status'], over['variance'], over['reason']), (c.OVERPAID, Decimal('7000'), 'health'))
        under = c.assess(breakdown(primary_count_raw=2, primary_amount=10000), Decimal('14000'), PARAMS)
        self.assertEqual((under['status'], under['variance']), (c.UNDERPAID, Decimal('-10000')))

    def test_penalties_are_deducted(self):
        b = breakdown(health_penalty=2000)
        self.assertEqual(c.assess(b, Decimal('12000'), PARAMS)['status'], c.MATCH)

    def test_missing_data_is_a_data_error(self):
        self.assertEqual(c.assess({}, Decimal('14000'), PARAMS)['reason'], 'NO_BREAKDOWN')
        self.assertEqual(c.assess(breakdown(), None, PARAMS)['reason'], 'NO_AMOUNT')
        self.assertEqual(c.assess(breakdown(), Decimal('14000'), None)['reason'], 'NOT_PCT_RULE')
