from django.db.models import Count, Q
from django.test import TestCase

from individual.models import GroupIndividual
from tasaf_payment.models import Paylist, PaylistItem, PaylistItemStatus
from tasaf_payment.reports import paylist_pdf_breakdown, paylist_pdf_rows


class PaylistPdfRowsDbTest(TestCase):
    def setUp(self):
        pending = Q(items__status=PaylistItemStatus.PENDING, items__is_deleted=False,
                    items__payment_account__group_beneficiary__isnull=False)
        self.paylist = (Paylist.objects.filter(is_deleted=False)
                        .annotate(n=Count('items', filter=pending)).filter(n__gt=1).order_by('-n', 'id').first())
        if self.paylist is None:
            self.skipTest('needs a paylist with two pending payees')
        self.items = list(self.paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING)
                          .select_related('payment_account__group_beneficiary__group')
                          .order_by('date_created', 'id'))

    def test_one_row_per_payee_with_its_account(self):
        rows = paylist_pdf_rows(self.paylist)
        first, account = rows[0], self.items[0].payment_account
        self.assertEqual(first['hhid'], account.group_beneficiary.group.code)
        self.assertEqual((first['fsp'], first['account']), (account.fsp_name, account.account_number))
        self.assertEqual(first['amount'], float(self.items[0].amount))

    def test_representative_is_the_primary_recipient(self):
        group_id = self.items[0].payment_account.group_beneficiary.group_id
        primary = (GroupIndividual.objects.filter(group_id=group_id, is_deleted=False, recipient_type='PRIMARY')
                   .select_related('individual').first())
        if primary is None:
            self.skipTest('the first payee has no PRIMARY recipient')
        expected = f"{primary.individual.first_name or ''} {primary.individual.last_name or ''}".strip()
        self.assertEqual(paylist_pdf_rows(self.paylist)[0]['representative'], expected)

    def test_withdrawn_payees_are_left_out(self):
        before = len(paylist_pdf_rows(self.paylist))
        PaylistItem.objects.filter(id=self.items[0].id).update(status=PaylistItemStatus.WITHDRAWN)
        self.assertEqual(len(paylist_pdf_rows(self.paylist)), before - 1)

    def assert_adds_up(self, breakdown):
        payments = sum(row['amount'] for row in breakdown['payments'])
        deductions = sum(row['amount'] for row in breakdown['deductions'])
        self.assertAlmostEqual(payments - deductions, breakdown['total'], places=2)
        self.assertAlmostEqual(breakdown['total'], sum(row['amount'] for row in paylist_pdf_rows(self.paylist)), places=2)

    def test_summary_adds_up_to_the_paylist_total(self):
        self.assert_adds_up(paylist_pdf_breakdown(self.paylist))

    def test_amount_the_breakdown_cannot_explain_is_shown_as_other(self):
        item = self.items[0]
        PaylistItem.objects.filter(id=item.id).update(amount=item.amount + 1000)
        breakdown = paylist_pdf_breakdown(self.paylist)
        self.assert_adds_up(breakdown)
        self.assertIn('other', {row['key'] for row in breakdown['payments']})
