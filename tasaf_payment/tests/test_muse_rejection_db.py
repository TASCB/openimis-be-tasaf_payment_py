import json
from collections import Counter
from unittest import mock

from django.db.models import Count, Q
from django.test import TestCase

from tasaf_payment import muse_inbound, muse_message
from tasaf_payment.models import (
    Paylist, PaylistItem, PaylistItemStatus, PaylistStatus, PaymentDestination, ReturnFeedback,
    ReturnFeedbackType, VerificationStatus,
)
from tasaf_payment.services import (
    _notify_payroll_if_all_closed, benefits_on_a_paylist, paid_elsewhere, unpayable_items,
    withdrawn_awaiting_payment,
)


def rejection(msg_id, status_desc):
    return {'message': {
        'messageHeader': {'msgId': 'muse-test-1', 'sender': 'MUSE', 'receiver': 'TASAFMIS',
                          'createdAt': '2026-10-09T15:13:55', 'messageType': 'BULK_PAYMENT_STATUS'},
        'messageDetails': {'status': 'Rejected', 'orgMsgId': msg_id, 'endToEndId': None,
                           'statusDesc': status_desc, 'orgReferenceNo': 'TP-TEST'}},
        'digitalSignature': 'x'}


class MuseRejectionDbTest(TestCase):
    def setUp(self):
        pending = Q(items__status=PaylistItemStatus.PENDING, items__is_deleted=False,
                    items__payment_account__group_beneficiary__isnull=False)
        self.paylist = (Paylist.objects.filter(is_deleted=False)
                        .annotate(n=Count('items', filter=pending)).filter(n__gt=0).order_by('-n').first())
        if self.paylist is None:
            self.skipTest('no paylist with pending payees in this database')
        Paylist.objects.filter(id=self.paylist.id).update(
            status=PaylistStatus.SUBMITTED, destination=PaymentDestination.MUSE,
            muse_msg_id='TMTESTREJECT0001', muse_batch_reference='TP-TEST', json_ext={})
        self.paylist.refresh_from_db()
        items = list(self.paylist.items.filter(is_deleted=False, status=PaylistItemStatus.PENDING)
                     .select_related('payment_account'))
        uses = Counter(i.payment_account.account_number for i in items)
        self.items = [i for i in items if uses[i.payment_account.account_number] == 1]
        if len(self.items) < 3:
            self.skipTest('need three payees with distinct accounts')
        self.bad, self.flaky, self.ok = self.items[:3]
        self.desc = (f'MSISDN invalid. for {self.bad.payment_account.account_number}, '
                     f'System internal error. for {self.flaky.payment_account.account_number}, '
                     f'MSISDN invalid. for 255799999999')

    def receive(self, desc=None):
        with self.captureOnCommitCallbacks(execute=False):
            return muse_inbound.handle(rejection('TMTESTREJECT0001', desc or self.desc))

    def test_rejection_withdraws_the_bad_payee_and_keeps_the_rest(self):
        ok, body = self.receive()
        self.assertTrue(ok)
        self.paylist.refresh_from_db()
        self.assertEqual(self.paylist.status, PaylistStatus.REJECTED)

        record = self.paylist.json_ext['muse_rejections'][0]
        self.assertEqual((record['withdrawn'], record['kept'], record['needs_review']), (1, 1, 1))

        bad = PaylistItem.objects.get(id=self.bad.id)
        self.assertEqual(bad.status, PaylistItemStatus.WITHDRAWN)
        self.assertEqual(bad.return_reason, 'MSISDN invalid')
        self.assertEqual(bad.payment_account.verification_status, VerificationStatus.FAILED)
        self.assertTrue(ReturnFeedback.objects.filter(
            paylist_item=bad, feedback_type=ReturnFeedbackType.REJECTED_IN_BATCH, reason_code='DATA').exists())
        self.assertEqual(PaylistItem.objects.get(id=self.flaky.id).status, PaylistItemStatus.PENDING)
        self.assertEqual(PaylistItem.objects.get(id=self.ok.id).status, PaylistItemStatus.PENDING)

    def test_case_management_gets_a_follow_up(self):
        from case_management.models import FollowUpRemark
        self.receive()
        follow_up = FollowUpRemark.objects.get(paylist_item_id=self.bad.id)
        self.assertEqual((follow_up.category, follow_up.priority, follow_up.status),
                         ('PAYMENT_FAILURE', 'HIGH', 'OPEN'))
        self.assertIn('MSISDN invalid', follow_up.remark)
        record = Paylist.objects.get(id=self.paylist.id).json_ext['muse_rejections'][0]
        self.assertEqual(record['payees'][0]['follow_up'], str(follow_up.id))

    def test_withdrawn_benefit_is_released_and_the_resend_excludes_it(self):
        self.receive()
        on_paylist = set(benefits_on_a_paylist().values_list('benefit_consumption_id', flat=True))
        self.assertNotIn(self.bad.benefit_consumption_id, on_paylist)
        self.assertIn(self.ok.benefit_consumption_id, on_paylist)

        paylist = Paylist.objects.get(id=self.paylist.id)
        sent = {r['payeeAccountNumber'] for r in muse_message.payee_rows(paylist)}
        self.assertNotIn(self.bad.payment_account.account_number, sent)
        self.assertIn(self.ok.payment_account.account_number, sent)
        self.assertFalse(paid_elsewhere(paylist).exists())

    def test_duplicate_verdict_changes_nothing(self):
        self.receive()
        before = Paylist.objects.get(id=self.paylist.id).json_ext
        self.receive()
        self.assertEqual(Paylist.objects.get(id=self.paylist.id).json_ext, before)

    def test_muse_time_is_tanzania_time(self):
        self.receive()
        at = Paylist.objects.get(id=self.paylist.id).muse_status_at
        self.assertEqual(at.replace(tzinfo=None).isoformat(), '2026-10-09T12:13:55')

    def test_inbound_log_keeps_the_whole_message(self):
        from muse_payment_adaptor.models import MuseTransactionLog
        long_desc = ', '.join(f'MSISDN invalid. for 2557000{n:05d}' for n in range(300))
        self.receive(long_desc)
        row = MuseTransactionLog.objects.filter(direction='IN').order_by('-id').first()
        self.assertEqual(json.loads(row.response_body)['message']['messageDetails']['statusDesc'], long_desc)

    def test_submit_refuses_an_account_no_longer_cleared(self):
        self.receive()
        paylist = Paylist.objects.get(id=self.paylist.id)
        account = self.ok.payment_account
        account.verification_status = VerificationStatus.MANUAL
        account.save(username='Admin')
        self.assertIn(self.ok.id, set(unpayable_items(paylist).values_list('id', flat=True)))
        self.assertNotIn(self.bad.id, set(unpayable_items(paylist).values_list('id', flat=True)))

    def test_correction_resolves_the_follow_up(self):
        from case_management.models import FollowUpRemark
        from case_management.services import PaymentChangeService
        from core.models import User
        self.receive()
        others = (Paylist.objects.filter(items__payment_account_id=self.bad.payment_account_id)
                  .exclude(id=self.paylist.id).values('id'))
        Paylist.objects.filter(id__in=others).update(status=PaylistStatus.REJECTED_AT_APPROVAL)
        user = User.objects.filter(username='Admin').first()
        number = '255712345678' if self.bad.payment_account.fsp_type == 'MOBILE' else '0150099988877'
        with mock.patch('case_management.services.CaseManagementConfig.payment_change_requires_approval', False), \
                mock.patch('case_management.services.CaseManagementConfig.enable_payment_change_approval', False), \
                self.captureOnCommitCallbacks(execute=False):
            result = PaymentChangeService(user).update_details(
                self.bad.payment_account_id, {'account_number': number}, reason_code='ACCOUNT_INVALID',
                reason_text='Number corrected after MUSE rejection')
        self.assertTrue(result.get('success'), result)
        follow_up = FollowUpRemark.objects.get(paylist_item_id=self.bad.id)
        self.assertEqual(follow_up.status, 'RESOLVED')

    def test_payroll_is_not_reconciled_while_a_withdrawn_payee_is_unpaid(self):
        if not self.paylist.payroll_id:
            self.skipTest('paylist has no payroll')
        self.receive()
        paylist = Paylist.objects.get(id=self.paylist.id)
        paylist.items.filter(status=PaylistItemStatus.PENDING).update(status=PaylistItemStatus.PROCESSED)
        Paylist.objects.filter(payroll_id=paylist.payroll_id).exclude(id=paylist.id).update(is_deleted=True)
        Paylist.objects.filter(id=paylist.id).update(status=PaylistStatus.CLOSED)
        paylist.refresh_from_db()
        self.assertEqual(list(withdrawn_awaiting_payment(paylist.payroll_id).values_list('id', flat=True)),
                         [self.bad.id])
        last_day, _ = self._release_day()
        with mock.patch('payroll.payments_registry.PaymentMethodStorage.get_chosen_payment_method') as strategy, \
                mock.patch('tasaf_payment.withdrawn_hold.today_eat', return_value=last_day):
            self.assertFalse(_notify_payroll_if_all_closed(paylist))
        strategy.assert_not_called()

    def test_withdrawn_payee_paid_on_a_later_paylist_releases_the_payroll(self):
        if not self.paylist.payroll_id:
            self.skipTest('paylist has no payroll')
        self.receive()
        self.assertTrue(withdrawn_awaiting_payment(self.paylist.payroll_id).exists())
        PaylistItem.objects.filter(id=self.ok.id).update(
            benefit_consumption_id=self.bad.benefit_consumption_id, status=PaylistItemStatus.PROCESSED)
        self.assertFalse(withdrawn_awaiting_payment(self.paylist.payroll_id).exists())

    def _release_day(self):
        from tasaf_payment.withdrawn_hold import deadline
        from datetime import timedelta
        item = PaylistItem.objects.get(id=self.bad.id)
        return deadline(self.paylist.payroll, item), deadline(self.paylist.payroll, item) + timedelta(days=1)

    def _only_the_withdrawn_payee_left(self):
        paylist = Paylist.objects.get(id=self.paylist.id)
        paylist.items.filter(status=PaylistItemStatus.PENDING).update(status=PaylistItemStatus.PROCESSED)
        Paylist.objects.filter(payroll_id=paylist.payroll_id).exclude(id=paylist.id).update(is_deleted=True)
        Paylist.objects.filter(id=paylist.id).update(status=PaylistStatus.CLOSED)
        paylist.refresh_from_db()
        return paylist

    def test_hold_lasts_until_the_cut_off_then_lapses(self):
        from case_management.models import FollowUpRemark
        from tasaf_payment.withdrawn_hold import LAPSED_KEY, lapse_expired
        if not self.paylist.payroll_id:
            self.skipTest('paylist has no payroll')
        self.receive()
        last_day, day_after = self._release_day()
        self.assertEqual(lapse_expired(self.paylist.payroll_id, today=last_day), [])
        self.assertTrue(withdrawn_awaiting_payment(self.paylist.payroll_id).exists())
        from tasaf_payment.gql_queries import PaylistGQLType
        hold = PaylistGQLType.resolve_withdrawn_hold(Paylist.objects.get(id=self.paylist.id), None)
        self.assertEqual((hold['withdrawn'], hold['awaiting'], hold['lapsed'], hold['deadline']),
                         (1, 1, 0, last_day.isoformat()))

        with self.captureOnCommitCallbacks(execute=False):
            lapsed = lapse_expired(self.paylist.payroll_id, today=day_after)
        self.assertEqual([i.id for i in lapsed], [self.bad.id])
        item = PaylistItem.objects.get(id=self.bad.id)
        self.assertEqual(item.status, PaylistItemStatus.WITHDRAWN)
        self.assertEqual(item.json_ext[LAPSED_KEY]['deadline'], last_day.isoformat())
        self.assertFalse(withdrawn_awaiting_payment(self.paylist.payroll_id).exists())
        self.assertIn(item.benefit_consumption_id,
                      set(PaylistItem.objects.filter(id=item.id).values_list('benefit_consumption_id', flat=True)))

        hold = PaylistGQLType.resolve_withdrawn_hold(Paylist.objects.get(id=self.paylist.id), None)
        self.assertEqual((hold['awaiting'], hold['lapsed']), (0, 1))
        parent = FollowUpRemark.objects.get(paylist_item_id=self.bad.id)
        self.assertEqual(parent.status, 'ESCALATED')
        self.assertIn('cycle cut-off', FollowUpRemark.objects.get(parent_remark_id=parent.id).remark)

    def test_release_hands_the_payroll_to_reconciliation_after_the_cut_off(self):
        from tasaf_payment.withdrawn_hold import release_expired_holds
        if not self.paylist.payroll_id:
            self.skipTest('paylist has no payroll')
        self.receive()
        self._only_the_withdrawn_payee_left()
        _, day_after = self._release_day()
        with mock.patch('payroll.payments_registry.PaymentMethodStorage.get_chosen_payment_method') as strategy, \
                self.captureOnCommitCallbacks(execute=False):
            planned = release_expired_holds(today=day_after, apply=False)
            self.assertIn(self.paylist.payroll_id, planned)
            self.assertTrue(withdrawn_awaiting_payment(self.paylist.payroll_id).exists())  # dry run
            strategy.assert_not_called()

            release_expired_holds(today=day_after)
        strategy.return_value.acknowledge_of_reponse_view.assert_called_once()

    def test_correction_still_clears_an_escalated_follow_up(self):
        from case_management.models import FollowUpRemark
        from case_management.services import PaymentChangeService
        from core.models import User
        from tasaf_payment.withdrawn_hold import lapse_expired
        if not self.paylist.payroll_id:
            self.skipTest('paylist has no payroll')
        self.receive()
        _, day_after = self._release_day()
        with self.captureOnCommitCallbacks(execute=False):
            lapse_expired(self.paylist.payroll_id, today=day_after)
        others = (Paylist.objects.filter(items__payment_account_id=self.bad.payment_account_id)
                  .exclude(id=self.paylist.id).values('id'))
        Paylist.objects.filter(id__in=others).update(status=PaylistStatus.REJECTED_AT_APPROVAL)
        number = '255712345678' if self.bad.payment_account.fsp_type == 'MOBILE' else '0150099988877'
        with mock.patch('case_management.services.CaseManagementConfig.payment_change_requires_approval', False), \
                mock.patch('case_management.services.CaseManagementConfig.enable_payment_change_approval', False), \
                self.captureOnCommitCallbacks(execute=False):
            PaymentChangeService(User.objects.filter(username='Admin').first()).update_details(
                self.bad.payment_account_id, {'account_number': number}, reason_code='ACCOUNT_INVALID',
                reason_text='Number corrected after the cut-off')
        self.assertEqual(FollowUpRemark.objects.get(paylist_item_id=self.bad.id, parent_remark__isnull=True).status,
                         'RESOLVED')


class UnappliedRepaymentDbTest(TestCase):
    setUp = MuseRejectionDbTest.setUp

    def unapply(self, item):
        from tasaf_payment.services import unapply_item
        with self.captureOnCommitCallbacks(execute=False):
            unapply_item(PaylistItem.objects.get(id=item.id), 'U02', 'Account closed')
        return PaylistItem.objects.get(id=item.id)

    def test_unapplied_payee_goes_to_correction(self):
        from case_management.models import FollowUpRemark
        item = self.unapply(self.bad)
        self.assertEqual(item.status, PaylistItemStatus.UNAPPLIED)
        self.assertIn('returned_at', item.json_ext)
        self.assertEqual(item.payment_account.verification_status, VerificationStatus.FAILED)
        self.assertEqual(item.payment_account.json_ext['muse_unapplied']['class'], 'UNAPPLIED')
        follow_up = FollowUpRemark.objects.get(paylist_item_id=item.id)
        self.assertEqual(str(follow_up.id), item.json_ext['follow_up_id'])
        self.assertIn('unapplied', follow_up.remark)

    def test_unapplied_benefit_is_released_and_held_for_repayment(self):
        item = self.unapply(self.bad)
        on_paylist = set(benefits_on_a_paylist().values_list('benefit_consumption_id', flat=True))
        self.assertNotIn(item.benefit_consumption_id, on_paylist)
        if self.paylist.payroll_id:
            self.assertIn(item.id, list(withdrawn_awaiting_payment(self.paylist.payroll_id).values_list('id', flat=True)))

    def test_older_unapplied_items_stay_final(self):
        PaylistItem.objects.filter(id=self.bad.id).update(status=PaylistItemStatus.UNAPPLIED, json_ext={})
        on_paylist = set(benefits_on_a_paylist().values_list('benefit_consumption_id', flat=True))
        self.assertIn(self.bad.benefit_consumption_id, on_paylist)

