from django.db.models import Count
from django.test import TestCase

from tasaf_payment.models import (Paylist, PaylistItem, PaylistStatus, PaymentAccount, PreAuditStatus,
                                  VerificationStatus)
from tasaf_payment.services import PaylistService, fsp_code_of


class GeneratePerFspDbTest(TestCase):
    def setUp(self):
        from core.models import User
        from payroll.models import BenefitConsumption, Payroll
        mixed = (PaylistItem.objects.filter(is_deleted=False, paylist__is_deleted=False, paylist__batch_type='MNO',
                                            paylist__payroll__isnull=False)
                 .values('paylist_id').annotate(n=Count('payment_account__fsp_name', distinct=True))
                 .filter(n__gte=2).values('paylist_id'))
        self.old = Paylist.objects.filter(id__in=mixed).first()
        if self.old is None:
            self.skipTest('no MNO paylist with payees of two FSPs in this database')
        self.user = User.objects.filter(username='Admin').first()
        Paylist.objects.filter(payroll_id=self.old.payroll_id).update(status=PaylistStatus.REJECTED_AT_APPROVAL)
        Payroll.objects.filter(id=self.old.payroll_id).update(status='APPROVE_FOR_PAYMENT')
        items = self.old.items.filter(is_deleted=False)
        BenefitConsumption.objects.filter(id__in=items.values('benefit_consumption_id')).update(status='ACCEPTED')
        PaymentAccount.objects.filter(id__in=items.values('payment_account_id')).update(
            verification_status=VerificationStatus.VERIFIED, pre_audit_status=PreAuditStatus.PASSED)

    def generate(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=False):
            return PaylistService(self.user).generate_sync(self.old.payroll_id, 'MNO', None, 'MUSE', **kwargs)

    def test_all_fsps_gives_one_paylist_per_fsp(self):
        result = self.generate()
        self.assertTrue(result['success'], result.get('error'))
        codes, seen = {}, []
        for row in result['paylists']:
            paylist = Paylist.objects.get(uuid=row['paylist_uuid'])
            names = set(paylist.items.values_list('payment_account__fsp_name', flat=True))
            self.assertEqual({fsp_code_of(n, codes) for n in names}, {row['fsp_code']})
            seen.append(row['fsp_code'])
        self.assertGreaterEqual(len(set(seen)), 2)

    def test_mixed_gives_one_paylist_with_every_fsp(self):
        result = self.generate(per_fsp=False)
        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(len(result['paylists']), 1)
        paylist = Paylist.objects.get(uuid=result['paylists'][0]['paylist_uuid'])
        self.assertGreaterEqual(paylist.items.values('payment_account__fsp_name').distinct().count(), 2)
