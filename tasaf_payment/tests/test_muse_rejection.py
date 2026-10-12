from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment import muse_rejection as mr

REAL_DA2C = 'Failure for 255739804505, System internal error. for 255726694556'
REAL_AC4F = ('MSISDN invalid. for 255646379525, MSISDN invalid. for 255721640936, '
             'MSISDN invalid. for 255647171873, MSISDN invalid. for 255721873384')
REAL_4182 = 'MSISDN invalid. for 255644279700, The receiver number must be a customer number. for 255767951263'

CLASSES = {'MSISDN invalid': 'DATA', 'The receiver number must be a customer number': 'DATA',
           'System internal error': 'TRANSIENT'}


class ParseTest(SimpleTestCase):
    def test_real_messages(self):
        self.assertEqual(mr.parse(REAL_DA2C), ([('Failure', '255739804505'),
                                                ('System internal error', '255726694556')], ''))
        failures, leftover = mr.parse(REAL_AC4F)
        self.assertEqual([f.account for f in failures],
                         ['255646379525', '255721640936', '255647171873', '255721873384'])
        self.assertEqual({f.reason for f in failures}, {'MSISDN invalid'})
        self.assertEqual(leftover, '')
        self.assertEqual(mr.parse(REAL_4182)[0][1],
                         ('The receiver number must be a customer number', '255767951263'))

    def test_batch_level_reason_names_nobody(self):
        self.assertEqual(mr.parse('Rejected: invalid payer account'), ([], 'Rejected: invalid payer account'))

    def test_bank_account_numbers(self):
        self.assertEqual(mr.parse('Account closed for 0150012345600')[0], [('Account closed', '0150012345600')])
        self.assertEqual(mr.parse('Invalid account for 24710A0012')[0], [('Invalid account', '24710A0012')])

    def test_a_word_is_never_an_account(self):
        self.assertEqual(mr.parse('Limit exceeded for customer')[0], [])

    def test_unreadable_tail_is_kept(self):
        failures, leftover = mr.parse('MSISDN invalid. for 255646379525, something else entirely')
        self.assertEqual(failures, [('MSISDN invalid', '255646379525')])
        self.assertEqual(leftover, 'something else entirely')

    def test_empty(self):
        self.assertEqual(mr.parse(None), ([], ''))


class ClassifyTest(SimpleTestCase):
    def test_known_reasons_ignore_case_spacing_and_period(self):
        self.assertEqual(mr.classify('MSISDN invalid', CLASSES), mr.DATA)
        self.assertEqual(mr.classify('msisdn  INVALID.', CLASSES), mr.DATA)
        self.assertEqual(mr.classify('System internal error', CLASSES), mr.TRANSIENT)

    def test_unlisted_is_unknown(self):
        self.assertEqual(mr.classify('Failure', CLASSES), mr.UNKNOWN)

    def test_transient_kept_until_the_limit(self):
        self.assertEqual(mr.decide(mr.TRANSIENT, 1, 2), mr.KEPT)
        self.assertEqual(mr.decide(mr.TRANSIENT, 2, 2), mr.WITHDRAWN)
        self.assertEqual(mr.decide(mr.DATA, 0, 2), mr.WITHDRAWN)
        self.assertEqual(mr.decide(mr.UNKNOWN, 0, 2), mr.WITHDRAWN)


class _Items:
    def __init__(self, items):
        self.items = items

    def filter(self, **kw):
        status = kw.get('status')
        return _Items([i for i in self.items if status is None or i.status == status])

    def select_related(self, *a):
        return self.items

    def exists(self):
        return bool(self.items)


def item(number, status='PENDING', **ext):
    i = SimpleNamespace(id=number, uuid=f'u-{number}', status=status, json_ext=dict(ext), saves=0,
                        payment_account=SimpleNamespace(account_number=number), payment_account_id=f'a-{number}')
    i.save = lambda **kw: setattr(i, 'saves', i.saves + 1)
    return i


def paylist(*items):
    p = SimpleNamespace(uuid='p', muse_msg_id='TMAC4FA5E22D9A01', muse_batch_reference='TP261009-AC4FA5E2',
                        json_ext={}, saves=0, items=_Items(list(items)))
    p.save = lambda **kw: setattr(p, 'saves', p.saves + 1)
    return p


class HandleRejectionTest(SimpleTestCase):
    def setUp(self):
        self.withdrawn = []
        for target, kwargs in (
            ('tasaf_payment.services._inbound_audit_user', {'return_value': object()}),
            ('tasaf_payment.events.emit_muse_event', {}),
            ('tasaf_payment.muse_rejection.withdraw_item',
             {'side_effect': lambda i, reason, cls, p, u: self.withdrawn.append((i.id, cls)) or 'fu-1'}),
            ('tasaf_payment.apps.TasafPaymentConfig.muse_rejection_reason_classes', {'new': CLASSES}),
            ('tasaf_payment.apps.TasafPaymentConfig.muse_transient_retry_limit', {'new': 2}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_data_errors_are_withdrawn_the_rest_stay(self):
        p = paylist(*(item(n) for n in ('255646379525', '255721640936', '255647171873',
                                         '255721873384', '255700000001')))
        record = mr.handle_batch_rejection(p, REAL_AC4F)
        self.assertEqual(len(self.withdrawn), 4)
        self.assertNotIn('255700000001', [w[0] for w in self.withdrawn])
        self.assertEqual((record['withdrawn'], record['kept'], record['needs_review']), (4, 0, 0))
        self.assertEqual(p.json_ext['muse_rejections'], [record])
        self.assertEqual(record['msg_id'], 'TMAC4FA5E22D9A01')

    def test_transient_is_kept_once_then_withdrawn(self):
        failing, internal = item('255739804505'), item('255726694556')
        p = paylist(failing, internal)
        first = mr.handle_batch_rejection(p, REAL_DA2C)
        self.assertEqual([r['action'] for r in first['payees']], [mr.WITHDRAWN, mr.KEPT])
        self.assertEqual(self.withdrawn, [('255739804505', mr.UNKNOWN)])
        self.assertEqual(internal.json_ext, {'muse_transient_strikes': 1})

        failing.status = 'WITHDRAWN'
        second = mr.handle_batch_rejection(p, 'System internal error. for 255726694556')
        self.assertEqual(second['payees'][0]['action'], mr.WITHDRAWN)
        self.assertEqual(len(p.json_ext['muse_rejections']), 2)

    def test_strike_resets_when_not_named_again(self):
        internal = item('255726694556', muse_transient_strikes=1)
        mr.handle_batch_rejection(paylist(internal, item('255646379525')),
                                  'MSISDN invalid. for 255646379525')
        self.assertEqual(internal.json_ext, {})

    def test_unknown_or_shared_account_is_never_dropped(self):
        p = paylist(item('255646379525'), item('255646379525'))
        record = mr.handle_batch_rejection(p, 'MSISDN invalid. for 255646379525, MSISDN invalid. for 255799999999')
        self.assertEqual(self.withdrawn, [])
        self.assertEqual([r['action'] for r in record['payees']], [mr.NEEDS_REVIEW, mr.NEEDS_REVIEW])
        self.assertEqual(record['needs_review'], 2)

    def test_only_pending_payees_match(self):
        record = mr.handle_batch_rejection(paylist(item('255646379525', status='WITHDRAWN')),
                                           'MSISDN invalid. for 255646379525')
        self.assertEqual(record['payees'][0]['action'], mr.NEEDS_REVIEW)

    def test_number_formats_are_normalised(self):
        mr.handle_batch_rejection(paylist(item('255646379525')), 'MSISDN invalid. for 0646379525')
        self.assertEqual(self.withdrawn, [('255646379525', mr.DATA)])

    def test_batch_level_reason_needs_review(self):
        record = mr.handle_batch_rejection(paylist(item('255646379525')), 'Rejected: invalid payer account')
        self.assertEqual((record['withdrawn'], record['needs_review']), (0, 1))
        self.assertEqual(record['unparsed'], 'Rejected: invalid payer account')
