import logging
import time
from datetime import datetime, timezone

from django.db import transaction

from tasaf_payment.gepg import message as gepg_message
from tasaf_payment.gepg import setup as gepg_setup

logger = logging.getLogger(__name__)

RETRYABLE_HTTP = (429, 500, 502, 503, 504)


def _config(name, default):
    from tasaf_payment.apps import TasafPaymentConfig
    value = getattr(TasafPaymentConfig, name, None)
    return default if value in (None, '') else value


def _now():
    return datetime.now(tz=timezone.utc)


def blocked():
    try:
        from coremis_app_integration.govesb import GovESBProducer  # noqa: F401
    except ImportError:
        return 'The GovESB adaptor is not installed; nothing was sent'
    if not gepg_setup.govesb_on():
        return 'GovESB is not configured on this server; nothing was sent'
    if not gepg_setup.api_code():
        return f'No GovESB API code for {gepg_setup.TOPIC}; nothing was sent'
    return _settings_missing()


def _settings_missing():
    values = gepg_setup.settings_values()
    if not all(values.get(f) for f in gepg_setup.SETTINGS_FIELDS):
        return 'GePG SP code and SP system ID are not set; nothing was sent'
    return None


def _headers():
    # Gepg-Code: spec §2 header, the SP code GePG issued (same value as SpCode)
    sp_code = gepg_setup.settings_values().get('sp_code')
    code = gepg_setup.service_code()
    return {**({'service-code': code} if code else {}), **({'Gepg-Code': sp_code} if sp_code else {})}


WITHDRAWN_KEY = 'verification_withdrawn'


def _withdraw(account, problem, at, user):
    from tasaf_payment.models import (
        MuseVerificationRecord, MuseVerificationResult, MuseVerificationType, VerificationStatus,
    )
    account.verification_status = VerificationStatus.FAILED
    account.json_ext = {**(account.json_ext or {}), WITHDRAWN_KEY: {**problem, 'at': at}}
    account.save(username=user.username)
    MuseVerificationRecord(
        payment_account=account, muse_reference='', verification_type=MuseVerificationType.MOBILE_VALIDATION,
        result=MuseVerificationResult.FAILED, failure_reason=problem['reason'],
        raw_response={'withdrawn': problem},
    ).save(username=user.username)


def _release(items, reason, user):
    from tasaf_payment.models import GepgItemStatus, PaymentAccount, VerificationStatus
    checks = [i for i in items if i.payment_account_id is None]
    if checks:
        from tasaf_payment.account_check import release
        release(checks, reason)
    for item in items:
        if item.payment_account_id is None:
            item.status = GepgItemStatus.RELEASED
            item.acc_sts_desc = reason[:255]
            item.save(update_fields=['status', 'acc_sts_desc'])
            continue
        account = PaymentAccount.objects.select_for_update().get(pk=item.payment_account_id)
        sent = (account.json_ext or {}).get('verification_sent') or {}
        if account.verification_status == VerificationStatus.PENDING_MUSE and \
                sent.get('gepg') == item.request.acc_verif_id:
            account.verification_status = item.previous_status
            account.save(user=user)
        item.status = GepgItemStatus.RELEASED
        item.acc_sts_desc = reason[:255]
        item.save(update_fields=['status', 'acc_sts_desc'])


class GepgVerificationSender:
    DISPATCHABLE_STATUSES = None

    def __init__(self, user, sleep=time.sleep):
        from tasaf_payment.models import VerificationStatus
        self.user = user
        self.sleep = sleep
        self.DISPATCHABLE_STATUSES = (VerificationStatus.PENDING, VerificationStatus.FAILED)
        self.withdrawn = 0
        self.withdrawn_accounts = []

    def dispatch(self, account_ids, after_change=False, before_first_payment=False):
        problem = blocked()
        if problem:
            return {'success': False, 'count': 0, 'batches': 0, 'skipped': 0, 'error': problem}
        requests, skipped = self._claim(account_ids, after_change, before_first_payment)
        sent, refused, errors = 0, 0, []
        for request in requests:
            outcome = self.send(request)
            if outcome is None:
                sent += request.item_count
            else:
                refused += request.item_count
                errors.append(outcome)
        logger.info("GePG verification: %d account(s) accepted in %d request(s), %d skipped, %d withdrawn, "
                    "%d failed", sent, len(requests) - len(errors), skipped, self.withdrawn, len(errors))
        return {'success': bool(sent) or not errors, 'count': sent, 'batches': len(requests) - len(errors),
                'refused': refused, 'refused_batches': len(errors), 'skipped': skipped,
                'withdrawn': self.withdrawn, 'error': '; '.join(errors[:3]) or None}

    def _statuses(self, before_first_payment):
        from tasaf_payment.models import VerificationStatus
        return (VerificationStatus.VERIFIED,) if before_first_payment else self.DISPATCHABLE_STATUSES

    @staticmethod
    def _due(accounts, before_first_payment):
        if not before_first_payment:
            return accounts
        from tasaf_payment.services import needs_first_payment_check
        return [a for a in accounts if needs_first_payment_check(a)]

    def _claim(self, account_ids, after_change, before_first_payment=False):
        from tasaf_payment.charges import resolve_fsp_code
        from tasaf_payment.models import (
            FspProfile, GepgVerificationItem, GepgVerificationRequest, PaymentAccount, PreAuditStatus,
            VerificationStatus,
        )
        from tasaf_payment import prefixes
        from tasaf_payment.events import emit_accounts_withdrawn
        from tasaf_payment.services import recipient_name

        size = int(_config('verification_batch_max_rows', 0) or 0) or 50000
        requests = []
        with transaction.atomic():
            accounts = self._due(list(PaymentAccount.objects.select_for_update(of=('self',)).filter(
                id__in=account_ids, is_deleted=False, verification_status__in=self._statuses(before_first_payment),
            ).select_related('group_beneficiary__group').order_by('id')), before_first_payment)
            skipped = len(set(account_ids)) - len(accounts)
            bics = {p.fsp_code: p.bic for p in FspProfile.objects.filter(is_deleted=False).exclude(bic='')}
            table = prefixes.table()
            sent_at = _now().isoformat()
            groups = {}
            for account in accounts:
                code = resolve_fsp_code(account.fsp_name) or ''
                if not bics.get(code):
                    skipped += 1
                    continue
                problem = prefixes.problem(account, table)
                if problem:
                    _withdraw(account, problem, sent_at, self.user)
                    self.withdrawn += 1
                    self.withdrawn_accounts.append(account)
                    continue
                groups.setdefault((code, account.fsp_type), []).append(account)
            for (code, fsp_type), members in sorted(groups.items()):
                for start in range(0, len(members), size):
                    part = members[start:start + size]
                    request = GepgVerificationRequest.objects.create(
                        acc_verif_id=gepg_message.new_acc_verif_id(), fsp_code=code, bic=bics[code],
                        item_count=len(part), requested_by=getattr(self.user, 'username', '') or '',
                        after_change=after_change)
                    request.fsp_type = fsp_type
                    items = []
                    for number, account in enumerate(part, start=1):
                        expected = recipient_name(account)
                        items.append(GepgVerificationItem(
                            request=request, item_id=str(number), payment_account=account,
                            acc_num=account.account_number or '', previous_status=account.verification_status,
                            expected_name=expected[:255]))
                        sent = {'expected_name': expected, 'sent_at': sent_at, 'gepg': request.acc_verif_id}
                        if after_change:
                            sent['after_change'] = True
                        if before_first_payment:
                            sent['before_first_payment'] = True
                            account.pre_audit_status = PreAuditStatus.PENDING
                        account.verification_status = VerificationStatus.PENDING_MUSE
                        ext = {**(account.json_ext or {}), 'verification_sent': sent}
                        ext.pop(WITHDRAWN_KEY, None)
                        account.json_ext = ext
                        account.save(username=self.user.username)
                    GepgVerificationItem.objects.bulk_create(items)
                    requests.append(request)
            emit_accounts_withdrawn(self.withdrawn_accounts, self.user)
        return requests, skipped

    def send(self, request):
        from tasaf_payment.models import GepgRequestStatus

        items = list(request.items.order_by('id'))
        body = gepg_message.build(request, gepg_setup.settings_values(), items)
        fmt = gepg_message.message_format()
        wire = gepg_message.serialize(body, fmt)
        request.request_body = wire
        problems = gepg_message.check(body, getattr(request, 'fsp_type', '') or self._fsp_type(items))
        if problems:
            text = '; '.join(f"{p['field']}: {p['message']} ({p['count']})"
                             for p in gepg_message.summarise(problems)[:6])
            return self._finish(request, items, GepgRequestStatus.REFUSED, f'GePG would reject this request — {text}')

        from coremis_app_integration.esb_client import DataFormat, ESBRequestType
        from coremis_app_integration.esb_client.exceptions import ESBConfigurationError, ESBSignatureError
        from coremis_app_integration.govesb import GovESBProducer

        headers = _headers()
        max_attempts = max(1, int(_config('gepg_send_max_attempts', 3)))
        backoff = float(_config('gepg_send_backoff_seconds', 2))
        error = 'not sent'
        for attempt in range(1, max_attempts + 1):
            request.attempts = attempt
            try:
                response = GovESBProducer().publish(
                    gepg_setup.TOPIC, wire, request_type=ESBRequestType.NORMAL,
                    data_format=DataFormat.XML if fmt == gepg_message.XML else DataFormat.JSON,
                    **({'extra_headers': headers} if headers else {}))
            except ESBConfigurationError as exc:
                return self._finish(request, items, GepgRequestStatus.FAILED, f'GovESB is misconfigured: {exc}')
            except ESBSignatureError as exc:
                request.sent_at = _now()
                return self._finish(request, items, GepgRequestStatus.SENT,
                                    f'GovESB reply signature could not be verified: {exc}', keep=True)
            except Exception as exc:  # noqa: BLE001 — transport / token errors are retried
                error = f'Could not reach GovESB: {exc}'
                if attempt < max_attempts:
                    self.sleep(backoff ** attempt)
                continue

            if not response.get('published'):
                return self._finish(request, items, GepgRequestStatus.REFUSED,
                                    'GovESB is not configured on this server; nothing was sent')
            status_code = response.get('status_code')
            request.esb_request_id = str(response.get('request_id') or '')[:100] or request.esb_request_id
            # GovESB reports "failed" whenever GePG's ack is not 7101; the ack still says why
            if response.get('ok') or gepg_message.find(response.get('esb_body'), gepg_message.REQUEST_ACK):
                request.sent_at = _now()
                return self._apply_ack(request, items, response.get('esb_body'))
            error = f"GovESB refused the request: {response.get('error') or f'HTTP {status_code}'}"
            if status_code in RETRYABLE_HTTP and attempt < max_attempts:
                self.sleep(backoff ** attempt)
                continue
            break
        return self._finish(request, items, GepgRequestStatus.FAILED, error)

    def _apply_ack(self, request, items, esb_body):
        from tasaf_payment.models import GepgRequestStatus

        loaded = gepg_message.load(esb_body)
        request.ack_body = loaded if isinstance(loaded, (dict, list)) else None
        ack = gepg_message.find(esb_body, gepg_message.REQUEST_ACK)
        if not ack:
            return self._finish(request, items, GepgRequestStatus.SENT, 'Sent; GePG returned no request ack',
                                keep=True)
        code = str(ack.get('AccVerifStsCode') or '')
        desc = str(ack.get('AccVerifStsDesc') or '')
        request.status_code = code
        if code == gepg_message.SUCCESS:
            return self._finish(request, items, GepgRequestStatus.ACCEPTED, desc, keep=True)
        return self._finish(request, items, GepgRequestStatus.REJECTED, f'GePG rejected the request: {code} {desc}')

    SEND_FIELDS = ('status', 'status_code', 'status_desc', 'attempts', 'esb_request_id', 'sent_at',
                   'request_body', 'ack_body')

    def _finish(self, request, items, status, desc, keep=False):
        from tasaf_payment.models import GepgRequestStatus, GepgVerificationRequest

        request.status = status
        request.status_desc = desc[:2000]
        with transaction.atomic():
            # GePG's answer can arrive before GovESB hands back the ack: keep it, save only the send
            answered = GepgVerificationRequest.objects.select_for_update().filter(
                pk=request.pk, status=GepgRequestStatus.ANSWERED).exists()
            if answered:
                request.status = GepgRequestStatus.ANSWERED
                keep = True
            request.save(update_fields=self.SEND_FIELDS)
            if not keep:
                _release(items, desc, self.user)
        logger.info("GePG verification %s -> %s %s", request.acc_verif_id, status, desc[:200])
        return None if keep else desc

    def fail(self, request, desc):
        from tasaf_payment.models import GepgRequestStatus
        return self._finish(request, list(request.items.order_by('id')), GepgRequestStatus.FAILED, desc)

    @staticmethod
    def _fsp_type(items):
        types = {i.payment_account.fsp_type for i in items if i.payment_account_id}
        return types.pop() if len(types) == 1 else ''

    def resend(self, acc_verif_id):
        ids, after_change = self.resend_accounts(acc_verif_id)
        return self.dispatch(ids, after_change=after_change)

    @staticmethod
    def resend_accounts(acc_verif_id):
        from tasaf_payment.models import GepgRequestStatus, GepgVerificationRequest
        request = GepgVerificationRequest.objects.filter(acc_verif_id=acc_verif_id).first()
        if not request:
            raise ValueError(f'No GePG verification request {acc_verif_id}')
        if request.check_job_id:
            raise ValueError(f'Request {acc_verif_id} belongs to a bulk account check; upload the file again')
        if request.status not in (GepgRequestStatus.FAILED, GepgRequestStatus.REFUSED,
                                  GepgRequestStatus.REJECTED, GepgRequestStatus.EXPIRED):
            raise ValueError(f'Request {acc_verif_id} is {request.status}; only failed, refused, '
                             f'rejected or expired requests can be sent again')
        return list(request.items.values_list('payment_account_id', flat=True)), request.after_change

    def preview(self, account_ids, sample_rows=20, before_first_payment=False):
        from tasaf_payment.charges import resolve_fsp_code
        from tasaf_payment.models import FspProfile, PaymentAccount

        accounts = self._due(list(PaymentAccount.objects.filter(
            id__in=account_ids, is_deleted=False, verification_status__in=self._statuses(before_first_payment),
        ).order_by('id')), before_first_payment)
        bics = {p.fsp_code: p.bic for p in FspProfile.objects.filter(is_deleted=False).exclude(bic='')}
        from tasaf_payment import prefixes
        settings = gepg_setup.settings_values()
        table = prefixes.table()
        groups, no_bic, withdrawn = {}, {}, []
        for account in accounts:
            code = resolve_fsp_code(account.fsp_name) or ''
            if not bics.get(code):
                no_bic[code or account.fsp_name] = no_bic.get(code or account.fsp_name, 0) + 1
                continue
            problem = prefixes.problem(account, table)
            if problem:
                withdrawn.append(problem)
                continue
            groups.setdefault((code, account.fsp_type), []).append(account)
        messages = []
        for (code, fsp_type), members in sorted(groups.items()):
            fake = type('R', (), {'acc_verif_id': '(assigned when sent)', 'bic': bics[code]})()
            shown = [type('I', (), {'item_id': str(n), 'acc_num': a.account_number or ''})()
                     for n, a in enumerate(members[:sample_rows], start=1)]
            body = gepg_message.build(fake, settings, shown)
            messages.append({'fsp_code': code, 'count': len(members), 'rows_shown': len(shown),
                             'body': gepg_message.serialize(body),
                             'problems': gepg_message.summarise(gepg_message.check(body, fsp_type))})
        from django.conf import settings as dj
        return {'channel': 'GEPG', 'format': gepg_message.message_format(), 'topic': gepg_setup.TOPIC,
                'service_code_set': bool(gepg_setup.service_code()),
                'api_code': gepg_setup.api_code() or None,
                'govesb_enabled': gepg_setup.govesb_on(),
                'esb_url': (getattr(dj, 'ESB', None) or {}).get('ENGINE_URL') if gepg_setup.govesb_on() else None,
                'request_type': 'NORMAL', 'accounts': len(accounts),
                'max_rows_per_message': int(_config('verification_batch_max_rows', 0) or 0),
                'messages': messages, 'skipped_no_bic': no_bic, 'withdrawn': len(withdrawn),
                'withdrawn_sample': [w['reason'] for w in withdrawn[:sample_rows]], 'blocked': blocked()}
