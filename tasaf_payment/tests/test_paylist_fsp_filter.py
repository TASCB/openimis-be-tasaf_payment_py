from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasaf_payment.charges import ChargeError
from tasaf_payment.services import only_fsp

CODES = {'CRDB Bank PLC': 'CRDB', 'NMB Bank': 'NMB'}


def resolve(name):
    if not name:
        raise ChargeError('NO_FSP_CODE', 'no name')
    return CODES.get(name, name.upper())


def pair(name):
    return (SimpleNamespace(id=name), SimpleNamespace(fsp_name=name))


@mock.patch('tasaf_payment.charges.resolve_fsp_code', side_effect=resolve)
class OnlyFspTest(SimpleTestCase):
    def test_no_code_keeps_every_pair(self, _):
        pairs = [pair('CRDB Bank PLC'), pair('NMB Bank')]
        self.assertEqual(only_fsp(pairs, None), pairs)

    def test_keeps_only_the_chosen_fsp(self, _):
        pairs = [pair('CRDB Bank PLC'), pair('NMB Bank'), pair('CRDB Bank PLC')]
        self.assertEqual([a.fsp_name for _, a in only_fsp(pairs, 'CRDB')], ['CRDB Bank PLC'] * 2)

    def test_unresolvable_name_never_matches(self, _):
        self.assertEqual(only_fsp([pair(None), pair('')], 'CRDB'), [])
