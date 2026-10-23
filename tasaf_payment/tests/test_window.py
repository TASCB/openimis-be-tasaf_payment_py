from django.test import TestCase

from tasaf_payment.window import household_of_benefit


class HouseholdOfBenefitTest(TestCase):
    def setUp(self):
        from individual.models import GroupIndividual
        from payroll.models import BenefitConsumption
        self.link = (GroupIndividual.objects.filter(is_deleted=False, role=GroupIndividual.Role.HEAD,
                                                    group__is_deleted=False).first())
        self.benefit = BenefitConsumption.objects.filter(is_deleted=False).first()
        if not self.link or not self.benefit:
            self.skipTest('needs a household head and a benefit')
        BenefitConsumption.objects.filter(id=self.benefit.id).update(individual_id=self.link.individual_id)

    def household(self):
        from payroll.models import BenefitConsumption
        return (BenefitConsumption.objects.filter(id=self.benefit.id)
                .annotate(h=household_of_benefit()).values_list('h', flat=True).get())

    def test_recorded_household_wins(self):
        from payroll.models import BenefitConsumption
        other = '00000000-0000-0000-0000-000000000001'
        BenefitConsumption.objects.filter(id=self.benefit.id).update(json_ext={'beneficiary_group_id': other})
        self.assertEqual(str(self.household()), other)

    def test_falls_back_to_the_payee_household(self):
        from payroll.models import BenefitConsumption
        BenefitConsumption.objects.filter(id=self.benefit.id).update(json_ext={})
        self.assertEqual(self.household(), self.link.group_id)
