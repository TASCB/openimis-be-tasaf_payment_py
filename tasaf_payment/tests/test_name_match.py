from contextlib import nullcontext
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment import name_match as nm
from tasaf_payment import services
from tasaf_payment.models import VerificationStatus


class NormaliseTest(SimpleTestCase):
    def test_case_accents_punctuation_and_titles(self):
        self.assertEqual(nm.normalise("Bi. Zainabu  O'Neil-Ally"), ['ZAINABU', 'O', 'NEIL', 'ALLY'])
        self.assertEqual(nm.normalise('Mr José Mbwana'), ['JOSE', 'MBWANA'])
        self.assertEqual(nm.normalise(None), [])
        self.assertEqual(nm.normalise(' .. '), [])


class DecideTest(SimpleTestCase):
    def assertDecision(self, expected, returned, surname, decision, reason):
        result = nm.decide(expected, returned, surname=surname)
        self.assertEqual((result.decision, result.reason), (decision, reason),
                         f'{expected!r} vs {returned!r}: score {result.score}')
        return result

    def test_word_order_and_missing_middle_name_still_verify(self):
        self.assertDecision('Emmanuel Akwilapo', 'AKWILAPO EMMANUEL', 'Akwilapo', nm.VERIFIED, 'NAME_MATCH')
        self.assertDecision('Emmanuel Akwilapo', 'EMMANUEL LEONARD AKWILAPO', 'Akwilapo',
                            nm.VERIFIED, 'NAME_MATCH')

    def test_spelling_variant_verifies(self):
        result = self.assertDecision('Mohamed Juma', 'MOHAMMED JUMA', 'Juma', nm.VERIFIED, 'NAME_MATCH')
        self.assertGreaterEqual(result.score, 80)
        self.assertDecision('Asha Hamis', 'ASHA HAMISI', 'Hamis', nm.VERIFIED, 'NAME_MATCH')

    def test_shared_first_name_only_fails(self):
        self.assertDecision('Juma Mohamed', 'JUMA ALLY', 'Mohamed', nm.FAILED, 'NAME_MISMATCH')
        self.assertDecision('Halima Said', 'AMINA SAID', 'Said', nm.FAILED, 'NAME_MISMATCH')

    def test_close_but_different_surname_goes_to_an_officer(self):
        self.assertDecision('Neema Joseph', 'NEEMA JOSEPHAT', 'Joseph', nm.MANUAL, 'SURNAME_MISMATCH')

    def test_single_word_returned_goes_to_an_officer(self):
        self.assertDecision('Emmanuel Akwilapo', 'EMMANUEL', 'Akwilapo', nm.MANUAL, 'TOO_FEW_WORDS_MATCHED')

    def test_initial_is_partial(self):
        result = self.assertDecision('Emmanuel Akwilapo', 'E. AKWILAPO', 'Akwilapo', nm.MANUAL, 'PARTIAL_MATCH')
        self.assertEqual(result.score, 75)

    def test_no_name_on_either_side(self):
        self.assertDecision('Emmanuel Akwilapo', '', 'Akwilapo', nm.FAILED, 'NO_NAME_RETURNED')
        self.assertDecision('', 'EMMANUEL AKWILAPO', '', nm.MANUAL, 'NO_EXPECTED_NAME')

    def test_account_in_another_members_name(self):
        result = nm.decide('Halima Said', 'JUMA SAID MWINYI', surname='Said',
                           others=[('SECONDARY', 'Juma Said Mwinyi')])
        self.assertEqual((result.decision, result.reason, result.member),
                         (nm.MANUAL, 'ANOTHER_MEMBER', 'SECONDARY'))

    def test_thresholds_are_configurable_and_recorded(self):
        result = nm.decide('Emmanuel Akwilapo', 'E. AKWILAPO', surname='Akwilapo',
                           overrides={'verified_min': 70, 'min_tokens_matched': 1})
        self.assertEqual(result.decision, nm.VERIFIED)
        self.assertEqual(result.thresholds['verified_min'], 70)
        self.assertEqual(result.method, nm.METHOD)


class InboundDecisionTest(SimpleTestCase):

    def _run(self, payload, json_ext=None, household=('Emmanuel Akwilapo', 'Akwilapo', [])):
        account = mock.MagicMock(json_ext=json_ext or {})
        with mock.patch.object(services.PaymentAccount.objects, 'get', return_value=account), \
                mock.patch.object(services, 'MuseVerificationRecord') as record, \
                mock.patch.object(services, '_inbound_audit_user', return_value='system'), \
                mock.patch.object(services.transaction, 'atomic', return_value=nullcontext()), \
                mock.patch.object(services, 'household_names', return_value=household), \
                mock.patch.object(services.MuseVerificationInboundService, '_thresholds', return_value={}):
            out = services.MuseVerificationInboundService().handle_result({'account_uuid': 'U1', **payload})
        self.assertTrue(out['success'], out)
        account.save.assert_called_once_with(user='system')
        record.return_value.save.assert_called_once_with(user='system')
        return account, record.call_args.kwargs

    def test_matching_name_verifies(self):
        account, record = self._run({'returned_name': 'AKWILAPO EMMANUEL LEONARD'})
        self.assertEqual(account.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(record['result'], 'PASSED')
        self.assertEqual((record['expected_name'], record['match_score']), ('Emmanuel Akwilapo', 100))
        self.assertEqual(record['match_details']['reason'], 'NAME_MATCH')

    def test_our_decision_overrides_a_gateway_pass(self):
        account, record = self._run({'returned_name': 'JUMA ALLY', 'result': 'PASSED'})
        self.assertEqual(account.verification_status, VerificationStatus.FAILED)
        self.assertIn('NAME_MISMATCH', record['failure_reason'])

    def test_gateway_failure_wins_and_names_are_kept(self):
        account, record = self._run({'returned_name': '', 'result': 'FAILED', 'failure_reason': 'Account not found'})
        self.assertEqual(account.verification_status, VerificationStatus.FAILED)
        self.assertEqual(record['failure_reason'], 'Account not found')
        self.assertIsNone(record['match_score'])
        self.assertEqual(record['expected_name'], 'Emmanuel Akwilapo')

    def test_judged_against_the_name_as_sent(self):
        account, record = self._run(
            {'returned_name': 'REHEMA MUSSA'},
            json_ext={'verification_sent': {'expected_name': 'Rehema Mussa'}},
            household=('Rehema Kombo', 'Kombo', []),
        )
        self.assertEqual(account.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(record['expected_name'], 'Rehema Mussa')

    def test_another_members_account_goes_to_an_officer(self):
        account, record = self._run({'returned_name': 'JUMA SAID MWINYI'},
                                    household=('Halima Said', 'Said', [('SECONDARY', 'Juma Said Mwinyi')]))
        self.assertEqual(account.verification_status, VerificationStatus.MANUAL)
        self.assertEqual(record['match_details']['member'], 'SECONDARY')

    def test_verdict_only_path_unchanged(self):
        account, record = self._run({'result': 'MANUAL', 'failure_reason': 'borderline'})
        self.assertEqual(account.verification_status, VerificationStatus.MANUAL)
        self.assertIsNone(record['expected_name'])
        self.assertIsNone(record['returned_name'])


class VerificationEventTest(SimpleTestCase):

    def _run(self, json_ext):
        account = mock.MagicMock(json_ext=json_ext)
        with mock.patch.object(services.PaymentAccount.objects, 'get', return_value=account), \
                mock.patch.object(services, 'MuseVerificationRecord'), \
                mock.patch.object(services, '_inbound_audit_user', return_value='system'), \
                mock.patch.object(services.transaction, 'atomic', return_value=nullcontext()), \
                mock.patch.object(services, 'emit_verification_event') as emit:
            services.MuseVerificationInboundService().handle_result(
                {'account_uuid': 'U1', 'result': 'FAILED', 'failure_reason': 'Account closed'})
        return account, emit

    def test_a_reply_to_an_account_sent_after_a_change_is_announced(self):
        account, emit = self._run({'verification_sent': {'after_change': True}})
        emit.assert_called_once_with(account, VerificationStatus.FAILED, 'Account closed')

    def test_other_replies_are_not_announced(self):
        _, emit = self._run({'verification_sent': {'expected_name': 'X'}})
        emit.assert_not_called()
