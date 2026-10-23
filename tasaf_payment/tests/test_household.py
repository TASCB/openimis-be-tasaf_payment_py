from unittest import mock

from django.test import SimpleTestCase, TestCase

from tasaf_payment import household as h
from tasaf_payment.tests.test_compliance import PARAMS, breakdown

RULE = 'calcrule_pct_payment.calculation_rule.PctPaymentCalculationRule._calculate_household_breakdown'


class HouseholdRecheckTest(SimpleTestCase):
    def test_unchanged_household_is_still_true(self):
        recorded = breakdown(young_child_count_raw=1, young_child_amount=7000)
        with mock.patch(RULE, return_value=dict(recorded)):
            result = h._recheck(None, PARAMS, None, recorded)
        self.assertEqual((result['status'], result['changed'], result['total']), (h.STILL_TRUE, [], 21000.0))

    def test_lost_disability_and_extra_child_are_named(self):
        recorded = breakdown(has_disability=True, disability_amount=5000)
        current = breakdown(young_child_count_raw=2)
        with mock.patch(RULE, return_value=current):
            result = h._recheck(None, PARAMS, None, recorded)
        self.assertEqual((result['status'], result['changed']), (h.CHANGED, ['disability', 'health']))
        self.assertEqual(result['total'], 28000.0)


class HouseholdLookupTest(TestCase):
    def setUp(self):
        from individual.models import Group
        from payment_cycle.models import PaymentCycle
        self.cycle = PaymentCycle.objects.filter(is_deleted=False).first()
        self.group = Group.objects.filter(is_deleted=False).exclude(code='').first()
        if not self.cycle or not self.group:
            self.skipTest('needs a payment cycle and a household')
        dup = mock.patch('tasaf_payment.household._duplicates', return_value=[])
        dup.start()
        self.addCleanup(dup.stop)

    def test_hhid_ignores_case_and_spaces(self):
        found = h.household_detail(self.cycle.id, hhid=f'  {self.group.code.lower()} ')
        self.assertEqual(found['group_id'], str(self.group.id))

    def test_unknown_hhid_is_none(self):
        self.assertIsNone(h.household_detail(self.cycle.id, hhid='NO-SUCH-HHID-0000'))

    def test_partial_hhid_matching_several_lists_them(self):
        from individual.models import Group
        prefix = self.group.code[:3]
        if Group.objects.filter(is_deleted=False, code__icontains=prefix).count() < 2:
            self.skipTest('needs two households sharing a prefix')
        found = h.household_detail(self.cycle.id, hhid=prefix)
        self.assertTrue(1 < len(found['matches']) <= h.MAX_MATCHES)
