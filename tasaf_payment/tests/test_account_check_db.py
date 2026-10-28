from unittest import mock

from django.test import TestCase

from tasaf_payment import account_check
from tasaf_payment.models import (
    AccountCheckResult, AccountCheckStatus, FspProfile, GepgItemStatus, GepgVerificationRequest, PaymentAccount,
)

CSV = """HHID,Name,Account number,FSP
P3-1,Asha Juma Mrisho,0111222333,CRDB
,Baraka Ali,0111222444,CRDB
P3-3,Chausiku Said,0111222333,CRDB
P3-4,,0111222555,CRDB
P3-5,Daudi Moshi,0111222666,NOSUCHBANK
"""


def answer(request, rows):
    return {'gepgAccVerifSubResp': {'AccVerifId': request.acc_verif_id, 'AccVerifItems': [
        {'AccVerifItemId': item.item_id, 'AccNum': item.acc_num, 'AccName': name, 'AccStsCode': code,
         'AccStsDesc': desc} for item, (name, code, desc) in zip(request.items.order_by('id'), rows)]}}


class AccountCheckDbTest(TestCase):
    def setUp(self):
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        if not FspProfile.objects.filter(fsp_code='CRDB', is_deleted=False).exists():
            FspProfile(fsp_code='CRDB', bank_name='CRDB Bank', fsp_type='BANK', bic='CORUTZTZ').save(user=self.user)
        FspProfile.objects.filter(fsp_code='CRDB').update(bic='CORUTZTZ', fsp_type='BANK', is_deleted=False)

    def job(self):
        return account_check.create_job(self.user, 'test.csv', CSV.encode())

    def test_upload_validates_rows(self):
        job = self.job()
        self.assertEqual((job.row_count, job.valid_count), (5, 2))
        problems = dict(job.rows.filter(result=AccountCheckResult.INVALID).values_list('row_number', 'problem'))
        self.assertEqual(problems[4], 'Duplicate of row 2')
        self.assertEqual(problems[5], 'No name')
        self.assertIn('Unknown FSP', problems[6])

    def test_missing_column_is_refused(self):
        with self.assertRaises(account_check.AccountCheckError):
            account_check.create_job(self.user, 'x.csv', b'HHID,Name\nP3-1,Asha\n')

    def test_row_limit(self):
        with mock.patch.object(account_check, 'max_rows', return_value=1):
            with self.assertRaises(account_check.AccountCheckError):
                self.job()

    def send(self, job):
        with mock.patch('tasaf_payment.gepg.sender.blocked', return_value=None), \
                mock.patch('tasaf_payment.gepg.sender.GepgVerificationSender.send', return_value=None), \
                self.captureOnCommitCallbacks(execute=False):
            account_check.send(self.user, job.uuid)
        return GepgVerificationRequest.objects.get(check_job=job)

    def test_answers_go_to_the_rows_only(self):
        from tasaf_payment.gepg import inbound
        accounts_before = PaymentAccount.objects.order_by('-date_updated').values_list('id', 'date_updated').first()
        job = self.job()
        request = self.send(job)
        self.assertEqual(request.items.count(), 2)
        self.assertTrue(all(i.payment_account_id is None for i in request.items.all()))
        with self.captureOnCommitCallbacks(execute=True):
            ok, _ = inbound.handle(answer(request, [('ASHA JUMA MRISHO', '7101', 'Successful'),
                                                    ('', '7230', 'Invalid account')]))
        self.assertTrue(ok)
        rows = {r.row_number: r for r in job.rows.all()}
        self.assertEqual(rows[2].result, AccountCheckResult.VERIFIED)
        self.assertEqual(rows[2].provider_name, 'ASHA JUMA MRISHO')
        self.assertEqual(rows[3].result, AccountCheckResult.NOT_FOUND)
        job.refresh_from_db()
        self.assertEqual(job.status, AccountCheckStatus.DONE)
        self.assertEqual(PaymentAccount.objects.order_by('-date_updated').values_list('id', 'date_updated').first(),
                         accounts_before)

    def test_unanswered_rows_expire(self):
        from tasaf_payment.gepg.sweeper import release_request
        job = self.job()
        request = self.send(job)
        with self.captureOnCommitCallbacks(execute=False):
            release_request(request, 'No GePG answer within 24 hours', self.user)
        self.assertFalse(request.items.filter(status=GepgItemStatus.PENDING).exists())
        self.assertEqual(set(job.rows.exclude(result=AccountCheckResult.INVALID).values_list('result', flat=True)),
                         {AccountCheckResult.NO_ANSWER})
        job.refresh_from_db()
        self.assertEqual(job.status, AccountCheckStatus.DONE)

    def test_sent_once_and_export(self):
        job = self.job()
        self.send(job)
        with self.assertRaises(account_check.AccountCheckError):
            account_check.send(self.user, job.uuid)
        from core.models import ExportableQueryModel
        name = account_check.export(self.user, job)
        text = ExportableQueryModel.objects.get(name=name).content.read().decode()
        self.assertTrue(text.startswith('HHID,Name (uploaded),Account number,FSP,Name at provider'))
        self.assertEqual(len(text.strip().splitlines()), 6)

    def test_old_checks_are_pruned(self):
        from datetime import datetime, timedelta, timezone
        from tasaf_payment.models import AccountCheckJob, AccountCheckRow
        old, waiting, recent = self.job(), self.job(), self.job()
        self.send(waiting)
        long_ago = datetime.now(tz=timezone.utc) - timedelta(days=91)
        AccountCheckJob.objects.filter(id__in=[old.id, waiting.id]).update(created_at=long_ago)
        self.assertEqual([r['id'] for r in account_check.prune()], [old.id])
        self.assertTrue(AccountCheckJob.objects.filter(id=old.id).exists())
        account_check.prune(apply=True)
        self.assertFalse(AccountCheckJob.objects.filter(id=old.id).exists())
        self.assertFalse(AccountCheckRow.objects.filter(job_id=old.id).exists())
        self.assertEqual(set(AccountCheckJob.objects.filter(id__in=[old.id, waiting.id, recent.id])
                             .values_list('id', flat=True)), {waiting.id, recent.id})

