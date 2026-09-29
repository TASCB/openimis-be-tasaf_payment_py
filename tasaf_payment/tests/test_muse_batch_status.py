from types import SimpleNamespace

from django.test import SimpleTestCase

from tasaf_payment.models import PaylistStatus as S
from tasaf_payment.services import _start_new_muse_attempt, apply_muse_batch_status


def paylist(status, **extra):
    p = SimpleNamespace(uuid='u', status=status, muse_status_desc=None, muse_status_at=None,
                        muse_msg_id='TMABC00000000001', json_ext={}, saves=0, **extra)
    p.save = lambda **kw: setattr(p, 'saves', p.saves + 1)
    return p


class MuseBatchStatusTest(SimpleTestCase):
    def apply(self, p, status):
        return apply_muse_batch_status(p, status, 'desc', user=object())

    def test_moves_forward_through_the_flow(self):
        p = paylist(S.SUBMITTED)
        for status in (S.RECEIVED, S.ACCEPTED, S.SENT_TO_BANK):
            self.assertTrue(self.apply(p, status))
            self.assertEqual(p.status, status)
        self.assertEqual((p.muse_status_desc, p.saves), ('desc', 3))

    def test_may_skip_a_stage(self):
        p = paylist(S.SUBMITTED)
        self.assertTrue(self.apply(p, S.ACCEPTED))

    def test_repeat_or_older_reply_is_ignored(self):
        p = paylist(S.ACCEPTED)
        self.assertFalse(self.apply(p, S.ACCEPTED))
        self.assertFalse(self.apply(p, S.RECEIVED))
        self.assertEqual((p.status, p.saves), (S.ACCEPTED, 0))

    def test_rejected_only_before_acceptance(self):
        self.assertTrue(self.apply(paylist(S.SUBMITTED), S.REJECTED))
        self.assertTrue(self.apply(paylist(S.RECEIVED), S.REJECTED))
        self.assertFalse(self.apply(paylist(S.ACCEPTED), S.REJECTED))

    def test_not_in_flight_is_ignored(self):
        for status in (S.APPROVED, S.CLOSED, S.REJECTED):
            self.assertFalse(self.apply(paylist(status), S.RECEIVED))

    def test_resubmitting_a_rejected_batch_is_a_new_message(self):
        p = paylist(S.REJECTED, muse_batch_reference='TP260904-A0F744FC')
        p.json_ext = {'muse_created_at': '2026-09-28 10:00:00'}
        _start_new_muse_attempt(p, user=object())
        self.assertEqual(p.json_ext, {'muse_attempt': 2})
        self.assertIsNone(p.muse_msg_id)
        self.assertEqual(p.muse_batch_reference, 'TP260904-A0F744FC')
