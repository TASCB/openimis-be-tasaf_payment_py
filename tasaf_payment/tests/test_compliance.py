from decimal import Decimal

from django.test import SimpleTestCase

from tasaf_payment import compliance as c

PARAMS = {
    'base_amount': Decimal('14000'), 'disability_top_up': Decimal('5000'),
    'young_child_unit': Decimal('7000'), 'young_child_cap_count': 3,
    'primary_unit': Decimal('5000'), 'primary_cap_count': 3,
    'secondary_unit': Decimal('8000'), 'secondary_cap_count': 3,
    'household_cap_amount': Decimal('50000'), 'children_cap_count': 3,
    'pregnant_woman_unit': Decimal('7000'), 'pregnant_woman_cap_count': 1,
}


def breakdown(**kw):
    out = {'base_amount': 14000, 'has_disability': False, 'disability_amount': 0,
           'young_child_count_raw': 0, 'young_child_count': 0, 'young_child_amount': 0,
           'primary_count_raw': 0, 'primary_count': 0, 'primary_amount': 0,
           'secondary_count_raw': 0, 'secondary_count': 0, 'secondary_amount': 0}
    out.update(kw)
    return out


class ComplianceAssessTest(SimpleTestCase):
    def test_pregnancy_is_paid_once_per_household(self):
        b = breakdown(pregnant_count_raw=2, pregnant_count=1, pregnant_amount=7000)
        result = c.assess(b, Decimal('21000'), PARAMS)
        self.assertEqual((result['status'], result['expected']['parts']['pregnancy']), (c.MATCH, Decimal('7000')))
        old = {k: v for k, v in breakdown().items()}
        self.assertEqual(c.assess(old, Decimal('14000'), PARAMS)['status'], c.MATCH)

    def test_disability_paid_without_the_flag_is_overpaid(self):
        b = breakdown(has_disability=False, disability_amount=5000)
        result = c.assess(b, Decimal('19000'), PARAMS)
        self.assertEqual((result['status'], result['reason'], result['variance']), (c.OVERPAID, 'disability', 5000))

    def test_disability_is_once_per_household_and_children_are_capped(self):
        b = breakdown(has_disability=True, disability_amount=5000, young_child_count_raw=4, young_child_count=3,
                      young_child_amount=21000)
        result = c.assess(b, Decimal('40000'), PARAMS)
        self.assertEqual(result['status'], c.MATCH)
        self.assertEqual(result['expected']['parts']['health'], Decimal('21000'))

    def test_household_cap_applies(self):
        b = breakdown(secondary_count_raw=3, secondary_amount=60000)
        result = c.assess(b, Decimal('50000'), {**PARAMS, 'secondary_unit': Decimal('20000')})
        self.assertEqual((result['status'], result['expected']['cap_cut']), (c.MATCH, Decimal('24000')))

    def test_three_children_in_total_highest_rate_first(self):
        b = breakdown(young_child_count_raw=2, young_child_amount=14000, primary_count_raw=2, primary_amount=10000,
                      secondary_count_raw=1, secondary_amount=8000)
        result = c.assess(b, Decimal('36000'), PARAMS)
        parts = result['expected']['parts']
        self.assertEqual((result['status'], parts['secondary'], parts['health'], parts['primary']),
                         (c.MATCH, Decimal('8000'), Decimal('14000'), Decimal('0')))

    def test_payment_under_the_old_per_category_cap_is_overpaid(self):
        b = breakdown(has_disability=True, disability_amount=5000, young_child_count_raw=3, young_child_amount=21000,
                      primary_count_raw=3, primary_amount=15000, secondary_count_raw=3, secondary_amount=24000)
        result = c.assess(b, Decimal('50000'), PARAMS)
        self.assertEqual((result['status'], result['variance'], result['reason']),
                         (c.OVERPAID, Decimal('7000'), 'health,primary'))

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
