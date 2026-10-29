from unittest import mock

from django.test import TestCase

from tasaf_payment.models import (
    MusePaymentType, Paylist, PaylistItem, PaylistItemStatus, PaylistStatus, PaymentAccount, PreAuditStatus,
    VerificationStatus,
)
from tasaf_payment.muse import inbound, message as mm, withdrawal
from tasaf_payment.tests import test_muse_rejection_db as rejection_tests


def payment_status(status, e2e, org_msg_id):
    return {'message': {
        'messageHeader': {'msgId': f'muse-{e2e}', 'sender': 'MUSE', 'receiver': 'TASAFMIS',
                          'createdAt': '2026-10-28 10:00:00', 'messageType': 'PAYMENT_STATUS'},
        'messageDetails': {'status': status, 'orgMsgId': org_msg_id, 'endtoEndId': e2e,
                           'statusDesc': 'Account closed', 'orgReferenceNo': 'TP-TEST'}},
        'digitalSignature': 'x'}


class UnappliedResendDbTest(TestCase):
    setUp_rejection = rejection_tests.MuseRejectionDbTest.setUp

    def setUp(self):
        from core.models import User
        from payroll.models import Payroll
        self.setUp_rejection()
        if not self.paylist.payroll_id:
            self.skipTest('paylist has no payroll')
        Payroll.objects.filter(id=self.paylist.payroll_id).update(status='APPROVE_FOR_PAYMENT')
        self.user = User.objects.filter(username='Admin').first()
        self.item = self.bad
        self.original_id = mm.end_to_end_id(self.item)

    def unapply(self, item):
        with self.captureOnCommitCallbacks(execute=False):
            inbound.handle(payment_status('UNAPPLIED', mm.end_to_end_id(item), 'TMTESTREJECT0001'))
        return PaylistItem.objects.get(id=item.id)

    def decide(self, item, action):
        from tasaf_payment.returned import decide_unapplied
        with self.captureOnCommitCallbacks(execute=False):
            decide_unapplied(self.user, item.uuid, action, 'test')
        return PaylistItem.objects.get(id=item.id)


    def clear(self, item):
        PaymentAccount.objects.filter(id=item.payment_account_id).update(
            verification_status=VerificationStatus.VERIFIED, pre_audit_status=PreAuditStatus.PASSED)

    def generate(self, resends_only=True):
        from tasaf_payment.services import PaylistService
        before = set(Paylist.objects.values_list('id', flat=True))
        with self.captureOnCommitCallbacks(execute=False):
            PaylistService(self.user)._generate_sync(self.paylist.payroll_id, self.paylist.batch_type, None, 'MUSE',
                                                     resends_only=resends_only)
        return (PaylistItem.objects.filter(benefit_consumption_id=self.item.benefit_consumption_id)
                .exclude(paylist_id__in=before).select_related('paylist').first())

    def test_repay_goes_as_an_unapplied_resend(self):
        self.clear(self.item)
        original = self.decide(self.unapply(self.item), 'REPAY')
        resend = self.generate()
        self.assertIsNotNone(resend)
        self.assertEqual(resend.paylist.payment_type, MusePaymentType.UNAPPLIED)
        self.assertEqual(resend.paylist.status, PaylistStatus.APPROVED)
        from approval.models import ApprovalRequest
        self.assertFalse(ApprovalRequest.objects.filter(object_id=str(resend.paylist_id)).exists())
        self.assertEqual(resend.org_end_to_end_id, self.original_id)
        self.assertNotEqual(resend.end_to_end_id, self.original_id)
        self.assertEqual(resend.amount, original.amount)
        self.assertFalse(PaylistItem.objects.filter(paylist=resend.paylist).exclude(
            org_end_to_end_id__isnull=False).exists())
        row = next(r for r in mm.payee_rows(resend.paylist) if r['endtoEndId'] == resend.end_to_end_id)
        self.assertEqual(row['orgEndToEndId'], self.original_id)
        self.assertEqual(mm.build(resend.paylist)['message']['messageHeader']['paymentType'], 'UNAPPLIED')

    def test_generate_leaves_the_resend_to_the_unapplied_page(self):
        self.clear(self.item)
        self.decide(self.unapply(self.item), 'REPAY')
        self.assertIsNone(self.generate(resends_only=False))
        self.assertIsNotNone(self.generate())

    def test_muse_answer_on_the_new_id_reaches_the_resend(self):
        self.clear(self.item)
        self.decide(self.unapply(self.item), 'REPAY')
        resend = self.generate()
        Paylist.objects.filter(id=resend.paylist_id).update(status=PaylistStatus.SUBMITTED, muse_msg_id='TMTESTRESEND0001')
        with self.captureOnCommitCallbacks(execute=False):
            inbound.handle(payment_status('UNAPPLIED/MNO', resend.end_to_end_id, None))
        resend.refresh_from_db()
        self.assertEqual(resend.status, PaylistItemStatus.UNAPPLIED)
        from tasaf_payment.returned import AWAITING_ACTION, resolution_of
        self.assertEqual(resolution_of(resend), AWAITING_ACTION)

    def test_withdrawn_payee_is_paid_as_a_new_payment_with_a_new_id(self):
        self.decide(self.unapply(self.item), 'WITHDRAW')
        self.clear(self.item)
        payment = self.generate(resends_only=False)
        self.assertIsNotNone(payment)
        self.assertEqual(payment.paylist.payment_type, MusePaymentType.PAYMENT)
        self.assertIsNone(payment.org_end_to_end_id)
        self.assertNotEqual(payment.end_to_end_id, self.original_id)

    def test_provider_change_withdraws_the_resend(self):
        from tasaf_payment.returned import UNAPPLIED_WITHDRAWN, resolution_of
        from tasaf_payment.unapplied import apply_provider_change
        self.clear(self.item)
        item = self.decide(self.unapply(self.item), 'CASE')
        account = item.payment_account
        with mock.patch('tasaf_payment.unapplied.provider_code', return_value='OTHER'):
            changed = apply_provider_change(account, self.user)
        self.assertEqual([i.id for i in changed], [item.id])
        item.refresh_from_db()
        self.assertEqual(item.json_ext['unapplied_action']['auto'], 'PROVIDER_CHANGED')
        self.assertEqual(resolution_of(item), UNAPPLIED_WITHDRAWN)

    def test_submit_refuses_a_resend_whose_amount_changed(self):
        from tasaf_payment.unapplied import resend_mismatches
        self.clear(self.item)
        self.decide(self.unapply(self.item), 'REPAY')
        resend = self.generate()
        self.assertEqual(resend_mismatches(resend.paylist), [])
        PaylistItem.objects.filter(id=resend.id).update(amount=(resend.amount or 0) + 1)
        self.assertEqual([i.id for i in resend_mismatches(resend.paylist)], [resend.id])

    def test_withdrawal_list_is_built_sent_and_acknowledged(self):
        item = self.decide(self.unapply(self.item), 'WITHDRAW')
        self.assertIn(item.id, list(withdrawal.pending().values_list('id', flat=True)))
        body = withdrawal.build([item], 'TWTEST')
        row = body['message']['withdrawalList'][0]
        self.assertEqual(row['orgEndToEndId'], self.original_id)
        self.assertEqual(body['message']['withdrawalSummary']['noofTransaction'], 1)
        with mock.patch('tasaf_payment.muse.sender._config', side_effect=lambda n, d: 'RECORD_ONLY' if n == 'muse_submit_mode' else d):
            message_id, count = withdrawal.send(self.user)
        self.assertGreaterEqual(count, 1)
        self.assertNotIn(item.id, list(withdrawal.pending().values_list('id', flat=True)))
        self.assertEqual(withdrawal.acknowledge(message_id, 'REJECTED', 'not known'), count)
        item.refresh_from_db()
        self.assertEqual(item.json_ext['muse_withdrawal']['status'], 'REJECTED')
        self.assertIn(item.id, list(withdrawal.pending().values_list('id', flat=True)))
