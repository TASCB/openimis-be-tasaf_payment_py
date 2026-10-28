import csv
import io
import logging
import uuid as _uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from django.db import transaction

from tasaf_payment.models import (
    AccountCheckJob, AccountCheckResult, AccountCheckRow, AccountCheckStatus,
)

logger = logging.getLogger(__name__)

HEADERS = {
    'hhid': ('hhid', 'household id', 'household'),
    'name': ('name', 'full name', 'account name'),
    'account_number': ('account number', 'account_number', 'accountnumber', 'account', 'phone', 'msisdn'),
    'fsp': ('fsp', 'fsp name', 'fsp_name', 'provider', 'bank'),
}
REQUIRED = ('name', 'account_number', 'fsp')
EXPORT_COLUMNS = ['HHID', 'Name (uploaded)', 'Account number', 'FSP', 'Name at provider', 'Match (%)',
                  'Status', 'Reason']
RESULT_LABELS = {
    AccountCheckResult.INVALID: 'Not sent (invalid row)',
    AccountCheckResult.WAITING: 'Waiting for GePG',
    AccountCheckResult.VERIFIED: 'Verified',
    AccountCheckResult.REVIEW: 'Check manually',
    AccountCheckResult.NAME_MISMATCH: 'Name mismatch',
    AccountCheckResult.NOT_FOUND: 'Account not found / invalid',
    AccountCheckResult.PROVIDER_ERROR: 'Provider error',
    AccountCheckResult.NO_ANSWER: 'No answer',
}


class AccountCheckError(Exception):
    pass


def max_rows():
    from tasaf_payment.apps import TasafPaymentConfig
    return int(getattr(TasafPaymentConfig, 'account_check_max_rows', 50000) or 50000)


# ── Upload ──────────────────────────────────────────────────────────────────

def _records(content):
    text = content.decode('utf-8-sig', errors='replace') if isinstance(content, bytes) else content
    sample = text[:4096]
    delimiter = ';' if sample.count(';') > sample.count(',') else ','
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if not header:
        raise AccountCheckError('The file is empty')
    keys = [h.strip().lower() for h in header]
    index = {}
    for field, names in HEADERS.items():
        for position, key in enumerate(keys):
            if key in names:
                index[field] = position
                break
    missing = [f for f in REQUIRED if f not in index]
    if missing:
        raise AccountCheckError('Missing column(s): ' + ', '.join(m.replace('_', ' ') for m in missing)
                                + '. Expected HHID, Name, Account number, FSP.')
    for line in reader:
        if not any(cell.strip() for cell in line):
            continue
        yield {f: (line[i].strip() if i < len(line) else '') for f, i in index.items()}


def _provider(fsp, cache):
    from tasaf_payment.charges import ChargeError, resolve_fsp_code
    from tasaf_payment.models import FspMapping, FspProfile
    key = fsp.strip().upper()
    if key not in cache:
        try:
            code = resolve_fsp_code(fsp) or ''
        except ChargeError:
            code = ''
        profile = FspProfile.objects.filter(is_deleted=False, fsp_code=code).first() if code else None
        mapped = bool(code) and FspMapping.objects.filter(is_deleted=False, fsp_code=code).exists()
        cache[key] = ((code if profile or mapped else ''), (profile.bic if profile else ''),
                      (profile.fsp_type if profile else ''), bool(profile))
    return cache[key]


def _problem(row, provider, seen, table):
    from tasaf_payment import msisdn, prefixes
    if not row['name']:
        return 'No name'
    if not row['account_number']:
        return 'No account number'
    if not row['fsp']:
        return 'No FSP'
    code, bic, fsp_type, set_up = provider
    if not code:
        return f"Unknown FSP '{row['fsp']}'"
    if not set_up:
        return f'{code} is not set up in FSP providers'
    if not bic:
        return f'{code} has no BIC in FSP providers'
    if fsp_type == 'MOBILE':
        mismatch = prefixes.problem(SimpleNamespace(fsp_type='MOBILE', account_number=row['account_number'],
                                                    fsp_name=row['fsp']), table, saved=code)
        if mismatch:
            return mismatch['reason'][:255]
        row['account_number'] = msisdn.normalise(row['account_number'])
    key = (code, row['account_number'])
    if key in seen:
        return f'Duplicate of row {seen[key]}'
    seen[key] = row['row_number']
    return ''


def create_job(user, file_name, content):
    from tasaf_payment import prefixes

    records = []
    for number, record in enumerate(_records(content), start=2):
        records.append({**record, 'row_number': number})
        if len(records) > max_rows():
            raise AccountCheckError(f'The file has more than {max_rows():,} rows; split it')
    if not records:
        raise AccountCheckError('The file has no rows')

    cache, seen, table = {}, {}, prefixes.table()
    rows = []
    for record in records:
        provider = _provider(record['fsp'], cache) if record['fsp'] else ('', '', '', False)
        problem = _problem(record, provider, seen, table)
        rows.append(AccountCheckRow(
            row_number=record['row_number'], hhid=record.get('hhid', '')[:100], name=record['name'][:255],
            account_number=record['account_number'][:50], fsp=record['fsp'][:100], fsp_code=provider[0],
            fsp_type=provider[2], problem=problem,
            result=AccountCheckResult.INVALID if problem else AccountCheckResult.WAITING))
    with transaction.atomic():
        job = AccountCheckJob.objects.create(
            file_name=(file_name or '')[:255], created_by=getattr(user, 'username', '') or '',
            created_by_user_id=str(getattr(user, 'id', '') or ''), row_count=len(rows),
            valid_count=sum(1 for r in rows if not r.problem))
        for row in rows:
            row.job = job
        AccountCheckRow.objects.bulk_create(rows, batch_size=2000)
    logger.info("Account check %s: %d row(s), %d valid", job.uuid, job.row_count, job.valid_count)
    return job


def summary(job, problems=200):
    from django.db.models import Count
    by_fsp = list(job.rows.exclude(result=AccountCheckResult.INVALID)
                  .values('fsp_code').annotate(rows=Count('id')).order_by('-rows'))
    by_result = dict(job.rows.values_list('result').annotate(n=Count('id')))
    invalid = list(job.rows.filter(result=AccountCheckResult.INVALID)
                   .values('row_number', 'hhid', 'name', 'account_number', 'fsp', 'problem')[:problems])
    return {'uuid': str(job.uuid), 'file_name': job.file_name, 'status': job.status,
            'row_count': job.row_count, 'valid_count': job.valid_count,
            'by_fsp': by_fsp, 'by_result': by_result, 'problems': invalid}


# ── Send ────────────────────────────────────────────────────────────────────

def queue_send(user, job_uuid):
    from tasaf_payment.gepg.sender import blocked
    from tasaf_payment.tasks import send_account_check_task

    problem = blocked()
    if problem:
        raise AccountCheckError(problem)
    job = AccountCheckJob.objects.filter(uuid=job_uuid).first()
    if not job:
        raise AccountCheckError('No such account check')
    if job.status != AccountCheckStatus.VALIDATED:
        raise AccountCheckError(f'This check is {job.status}; it was already sent')
    if not job.valid_count:
        raise AccountCheckError('No valid row to send')
    try:
        send_account_check_task.delay(str(job.uuid), user.id)
        return None
    except Exception:  # noqa: BLE001 — no broker: send inline
        logger.warning("Account check %s: enqueue failed, sending inline", job.uuid, exc_info=True)
    return send(user, job.uuid)


def send(user, job_uuid):
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.gepg import message as gepg_message
    from tasaf_payment.gepg.sender import GepgVerificationSender, blocked
    from tasaf_payment.models import FspProfile, GepgVerificationItem, GepgVerificationRequest

    problem = blocked()
    if problem:
        raise AccountCheckError(problem)
    size = int(getattr(TasafPaymentConfig, 'verification_batch_max_rows', 0) or 0) or 50000
    requests = []
    with transaction.atomic():
        job = AccountCheckJob.objects.select_for_update().filter(uuid=job_uuid).first()
        if not job:
            raise AccountCheckError('No such account check')
        if job.status != AccountCheckStatus.VALIDATED:
            raise AccountCheckError(f'This check is {job.status}; it was already sent')
        if not job.valid_count:
            raise AccountCheckError('No valid row to send')
        bics = {p.fsp_code: p.bic for p in FspProfile.objects.filter(is_deleted=False).exclude(bic='')}
        groups = {}
        for row in job.rows.filter(result=AccountCheckResult.WAITING).order_by('row_number'):
            groups.setdefault((row.fsp_code, row.fsp_type), []).append(row)
        for (code, fsp_type), members in sorted(groups.items()):
            for start in range(0, len(members), size):
                part = members[start:start + size]
                request = GepgVerificationRequest.objects.create(
                    acc_verif_id=gepg_message.new_acc_verif_id(), fsp_code=code, bic=bics.get(code, ''),
                    item_count=len(part), requested_by=job.created_by, check_job=job)
                request.fsp_type = fsp_type
                items = GepgVerificationItem.objects.bulk_create([
                    GepgVerificationItem(request=request, item_id=str(n), acc_num=row.account_number,
                                         expected_name=row.name)
                    for n, row in enumerate(part, start=1)], batch_size=2000)
                for row, item in zip(part, items):
                    row.gepg_item = item
                AccountCheckRow.objects.bulk_update(part, ['gepg_item'], batch_size=2000)
                requests.append(request)
        job.status = AccountCheckStatus.SENT
        job.sent_at = datetime.now(tz=timezone.utc)
        job.save(update_fields=['status', 'sent_at'])

    sender = GepgVerificationSender(user)
    refused = [error for error in (sender.send(r) for r in requests) if error]
    complete(job.id)
    return {'requests': len(requests), 'refused': len(refused), 'error': '; '.join(refused[:3]) or None}


# ── GePG answers ────────────────────────────────────────────────────────────

def apply_answer(item, code, desc, name, retry_codes):
    from tasaf_payment import name_match
    from tasaf_payment.gepg import message as gepg_message

    row = AccountCheckRow.objects.filter(gepg_item=item).first()
    if row is None:
        item.decision = 'IGNORED'
        return
    row.provider_name, row.answered_at = name[:150], datetime.now(tz=timezone.utc)
    if code in retry_codes:
        row.result, row.reason = AccountCheckResult.PROVIDER_ERROR, f'GePG {code}: {desc or "not checked"}'[:255]
    elif code != gepg_message.SUCCESS or not name:
        row.result = AccountCheckResult.NOT_FOUND
        row.reason = f"GePG {code or '(no code)'}: {desc or 'account could not be verified'}"[:255]
    else:
        match = name_match.decide(row.name, name)
        row.match_score = match.score
        row.result = {name_match.VERIFIED: AccountCheckResult.VERIFIED,
                      name_match.MANUAL: AccountCheckResult.REVIEW}.get(match.decision,
                                                                       AccountCheckResult.NAME_MISMATCH)
        row.reason = '' if row.result == AccountCheckResult.VERIFIED else match.reason.replace('_', ' ').lower()
    row.save()
    item.decision = row.result[:10]


def release(items, reason):
    AccountCheckRow.objects.filter(gepg_item__in=items, result=AccountCheckResult.WAITING).update(
        result=AccountCheckResult.NO_ANSWER, reason=reason[:255], answered_at=datetime.now(tz=timezone.utc))


def complete(job_id):
    from tasaf_payment.events import emit_account_check_done
    with transaction.atomic():
        job = AccountCheckJob.objects.select_for_update().filter(id=job_id, status=AccountCheckStatus.SENT).first()
        if not job or job.rows.filter(result=AccountCheckResult.WAITING).exists():
            return False
        job.status = AccountCheckStatus.DONE
        job.finished_at = datetime.now(tz=timezone.utc)
        job.save(update_fields=['status', 'finished_at'])
        emit_account_check_done(job)
    logger.info("Account check %s done", job.uuid)
    return True


# ── Retention ───────────────────────────────────────────────────────────────

def retention_days():
    from tasaf_payment.apps import TasafPaymentConfig
    return int(getattr(TasafPaymentConfig, 'account_check_retention_days', 90) or 90)


def prune(days=None, apply=False):
    from datetime import timedelta
    from tasaf_payment.models import GepgVerificationItem, GepgVerificationRequest

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days or retention_days())
    old = AccountCheckJob.objects.filter(created_at__lt=cutoff).exclude(status=AccountCheckStatus.SENT)
    rows = list(old.values('id', 'uuid', 'file_name', 'created_by', 'created_at', 'row_count'))
    if apply and rows:
        ids = [r['id'] for r in rows]
        with transaction.atomic():
            AccountCheckRow.objects.filter(job_id__in=ids).delete()
            GepgVerificationItem.objects.filter(request__check_job_id__in=ids).delete()
            GepgVerificationRequest.objects.filter(check_job_id__in=ids).delete()
            AccountCheckJob.objects.filter(id__in=ids).delete()
        logger.info("Account checks pruned: %d job(s) older than %s", len(rows), cutoff.date())
    return rows


# ── Download ────────────────────────────────────────────────────────────────

def export(user, job):
    from django.core.files.base import ContentFile
    from core.models import ExportableQueryModel

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    for row in job.rows.order_by('row_number').iterator(chunk_size=2000):
        writer.writerow([row.hhid, row.name, row.account_number, row.fsp_code or row.fsp, row.provider_name,
                         '' if row.match_score is None else row.match_score,
                         RESULT_LABELS.get(row.result, row.result), row.problem or row.reason])
    filename = f"{_uuid.uuid4()}.csv"
    record = ExportableQueryModel(
        name=filename, model='AccountCheckRow', content=ContentFile(out.getvalue().encode('utf-8'), filename),
        user=user, sql_query=f'tasaf_payment.account_check.export {job.uuid}',
        file_format=ExportableQueryModel.FileFormat.CSV)
    record.save()
    return record.name
