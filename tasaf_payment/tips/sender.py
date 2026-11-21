import logging
import time
from datetime import datetime, timezone

from django.db import transaction

from tasaf_payment.tips import message as tips_message
from tasaf_payment.tips import setup as tips_setup

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
    if not tips_setup.govesb_on():
        return 'GovESB is not configured on this server; nothing was sent'
    if not tips_setup.api_code(tips_message.BULK):
        return f'No GovESB API code for {tips_message.TOPICS[tips_message.BULK]}; nothing was sent'
    values = tips_setup.settings_values()
    if not all(values.get(f) for f in tips_setup.SETTINGS_FIELDS):
        return 'TIPS sender, receiver and service codes are not set; nothing was sent'
    return None


def identifier_of(account):
    kind = 'MSISDN' if (account.fsp_type or '').upper() == 'MOBILE' else 'BANK'
    return kind, tips_message.normalise_identifier(kind, account.account_number)


def release_items(items, reason, user):
    from tasaf_payment.models import GepgItemStatus, PaymentAccount, VerificationStatus
    for item in items:
        if item.payment_account_id:
            account = PaymentAccount.objects.select_for_update().get(pk=item.payment_account_id)
            sent = (account.json_ext or {}).get('verification_sent') or {}
            if account.verification_status == VerificationStatus.PENDING_MUSE and \
                    sent.get('tips') == item.request.msg_id:
                account.verification_status = item.previous_status
                account.save(user=user)
        item.status = GepgItemStatus.RELEASED
        item.error_description = reason[:255]
        item.save(update_fields=['status', 'error_description'])


class TipsLookupSender:
    def __init__(self, user, sleep=time.sleep):
        from tasaf_payment.models import VerificationStatus
        self.user = user
        self.sleep = sleep
        self.statuses = (VerificationStatus.PENDING, VerificationStatus.FAILED)

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
        logger.info("TIPS lookup: %d account(s) in %d request(s), %d skipped, %d failed", sent,
                    len(requests) - len(errors), skipped, len(errors))
        return {'success': bool(sent) or not errors, 'count': sent, 'batches': len(requests) - len(errors),
                'refused': refused, 'refused_batches': len(errors), 'skipped': skipped, 'withdrawn': 0,
                'error': '; '.join(errors[:3]) or None}

    def _accounts(self, account_ids, before_first_payment, lock):
        from tasaf_payment.models import PaymentAccount, VerificationStatus
        from tasaf_payment.services import needs_first_payment_check
        qs = PaymentAccount.objects.filter(
            id__in=account_ids, is_deleted=False,
            verification_status__in=(VerificationStatus.VERIFIED,) if before_first_payment else self.statuses)
        if lock:
            qs = qs.select_for_update(of=('self',))
        accounts = list(qs.select_related('group_beneficiary__group').order_by('id'))
        return [a for a in accounts if needs_first_payment_check(a)] if before_first_payment else accounts

    @staticmethod
    def _groups(accounts):
        from tasaf_payment.charges import resolve_fsp_code
        codes = tips_setup.settings_values().get('fsp_codes') or {}
        groups, skipped = {}, {}
        for account in accounts:
            code = resolve_fsp_code(account.fsp_name) or ''
            tips_code = codes.get(code)
            if not tips_code:
                skipped[code or account.fsp_name] = skipped.get(code or account.fsp_name, 0) + 1
                continue
            groups.setdefault((code, tips_code), []).append(account)
        return groups, skipped

    def _claim(self, account_ids, after_change, before_first_payment=False):
        from tasaf_payment.models import PreAuditStatus, TipsLookupItem, TipsLookupRequest, VerificationStatus
        from tasaf_payment.services import recipient_name

        size = max(1, int(_config('tips_bulk_max_rows', 1000)))
        single_ok = bool(tips_setup.api_code(tips_message.SINGLE))
        requests = []
        with transaction.atomic():
            accounts = self._accounts(account_ids, before_first_payment, lock=True)
            groups, no_code = self._groups(accounts)
            skipped = len(set(account_ids)) - len(accounts) + sum(no_code.values())
            sent_at = _now().isoformat()
            for (code, tips_code), members in sorted(groups.items()):
                for start in range(0, len(members), size):
                    part = members[start:start + size]
                    request = TipsLookupRequest.objects.create(
                        msg_id=tips_message.new_msg_id(),
                        kind=tips_message.SINGLE if len(part) == 1 and single_ok else tips_message.BULK,
                        fsp_code=code, tips_code=tips_code, item_count=len(part),
                        requested_by=getattr(self.user, 'username', '') or '', after_change=after_change)
                    items = []
                    for account in part:
                        kind, number = identifier_of(account)
                        expected = recipient_name(account)
                        items.append(TipsLookupItem(
                            request=request, payment_account=account, identifier_type=kind, identifier=number,
                            fsp_code=tips_code, previous_status=account.verification_status,
                            expected_name=expected[:255]))
                        sent = {'expected_name': expected, 'sent_at': sent_at, 'tips': request.msg_id}
                        if after_change:
                            sent['after_change'] = True
                        if before_first_payment:
                            sent['before_first_payment'] = True
                            account.pre_audit_status = PreAuditStatus.PENDING
                        account.verification_status = VerificationStatus.PENDING_MUSE
                        account.json_ext = {**(account.json_ext or {}), 'verification_sent': sent}
                        account.save(username=self.user.username)
                    TipsLookupItem.objects.bulk_create(items)
                    requests.append(request)
        return requests, skipped

    @staticmethod
    def build(request, items):
        values = tips_setup.settings_values()
        if request.kind == tips_message.SINGLE:
            item = items[0]
            return tips_message.build_single(values, request.msg_id, item.identifier_type, item.identifier,
                                             item.fsp_code)
        return tips_message.build_bulk(values, request.msg_id,
                                       [(i.identifier_type, i.identifier, i.fsp_code) for i in items])

    def send(self, request):
        from tasaf_payment.models import TipsRequestStatus

        items = list(request.items.order_by('id'))
        body = self.build(request, items)
        request.request_body = body
        problems = tips_message.check(body, request.kind)
        if problems:
            text = '; '.join(f"{p['field']}: {p['message']}" for p in problems[:6])
            return self._finish(request, items, TipsRequestStatus.REFUSED, f'TIPS would reject this lookup — {text}')

        from coremis_app_integration.esb_client import DataFormat, ESBRequestType
        from coremis_app_integration.esb_client.exceptions import ESBConfigurationError, ESBSignatureError
        from coremis_app_integration.govesb import GovESBProducer

        kind = request.kind
        headers = tips_message.headers(tips_setup.settings_values(), kind)
        max_attempts = max(1, int(_config('tips_send_max_attempts', 3)))
        error = 'not sent'
        for attempt in range(1, max_attempts + 1):
            request.attempts = attempt
            try:
                response = GovESBProducer().publish(
                    tips_message.TOPICS[kind], body, request_type=ESBRequestType.NORMAL,
                    data_format=DataFormat.JSON, **({'extra_headers': headers} if headers else {}))
            except ESBConfigurationError as exc:
                return self._finish(request, items, TipsRequestStatus.FAILED, f'GovESB is misconfigured: {exc}')
            except ESBSignatureError as exc:
                request.sent_at = _now()
                return self._finish(request, items, TipsRequestStatus.SENT,
                                    f'GovESB reply signature could not be verified: {exc}', keep=True)
            except Exception as exc:  # noqa: BLE001 — transport / token errors are retried
                error = f'Could not reach GovESB: {exc}'
                if attempt < max_attempts:
                    self.sleep(2 ** attempt)
                continue

            if not response.get('published'):
                return self._finish(request, items, TipsRequestStatus.REFUSED,
                                    'GovESB is not configured on this server; nothing was sent')
            request.esb_request_id = str(response.get('request_id') or '')[:100] or request.esb_request_id
            if response.get('ok'):
                request.sent_at = _now()
                reply = response.get('esb_body')
                request.ack_body = tips_message.without_identity(reply) if isinstance(reply, (dict, list)) else None
                results = tips_message.results_in(reply)
                if results:
                    from tasaf_payment.tips.inbound import apply_results
                    self._finish(request, items, TipsRequestStatus.ACCEPTED, 'Answered in the reply', keep=True)
                    apply_results(request.msg_id, results, reply)
                    return None
                return self._finish(request, items, TipsRequestStatus.ACCEPTED, 'Sent; waiting for the TIPS result',
                                    keep=True)
            status_code = response.get('status_code')
            error = f"GovESB refused the lookup: {response.get('error') or f'HTTP {status_code}'}"
            if status_code in RETRYABLE_HTTP and attempt < max_attempts:
                self.sleep(2 ** attempt)
                continue
            break
        return self._finish(request, items, TipsRequestStatus.FAILED, error)

    SEND_FIELDS = ('status', 'status_desc', 'attempts', 'esb_request_id', 'sent_at', 'request_body', 'ack_body')

    def _finish(self, request, items, status, desc, keep=False):
        from tasaf_payment.models import TipsLookupRequest, TipsRequestStatus

        request.status = status
        request.status_desc = desc[:2000]
        with transaction.atomic():
            if TipsLookupRequest.objects.select_for_update().filter(
                    pk=request.pk, status=TipsRequestStatus.ANSWERED).exists():
                request.status = TipsRequestStatus.ANSWERED
                keep = True
            request.save(update_fields=self.SEND_FIELDS)
            if not keep:
                release_items(items, desc, self.user)
        logger.info("TIPS lookup %s -> %s %s", request.msg_id, status, desc[:200])
        return None if keep else desc

    def resend(self, msg_id):
        from tasaf_payment.models import TipsLookupRequest, TipsRequestStatus
        request = TipsLookupRequest.objects.filter(msg_id=msg_id).first()
        if not request:
            raise ValueError(f'No TIPS lookup {msg_id}')
        if request.status not in (TipsRequestStatus.FAILED, TipsRequestStatus.REFUSED, TipsRequestStatus.EXPIRED):
            raise ValueError(f'Lookup {msg_id} is {request.status}; only failed, refused or expired lookups '
                             f'can be sent again')
        ids = list(request.items.values_list('payment_account_id', flat=True))
        return self.dispatch(ids, after_change=request.after_change)

    def preview(self, account_ids, sample_rows=20, before_first_payment=False):
        accounts = self._accounts(account_ids, before_first_payment, lock=False)
        groups, no_code = self._groups(accounts)
        values = tips_setup.settings_values()
        size = max(1, int(_config('tips_bulk_max_rows', 1000)))
        messages = []
        for (code, tips_code), members in sorted(groups.items()):
            shown = [(*identifier_of(a), tips_code) for a in members[:sample_rows]]
            if len(members) == 1 and tips_setup.api_code(tips_message.SINGLE):
                body = tips_message.build_single(values, '(assigned when sent)', *shown[0])
                kind = tips_message.SINGLE
            else:
                body = tips_message.build_bulk(values, '(assigned when sent)', shown)
                kind = tips_message.BULK
            problems = [p for p in tips_message.check(body, kind) if p['field'] != 'msgId']
            messages.append({'fsp_code': code, 'count': len(members), 'rows_shown': len(shown), 'body': body,
                             'problems': [{'field': p['field'], 'message': p['message'], 'count': 1}
                                          for p in problems]})
        from django.conf import settings as dj
        on = tips_setup.govesb_on()
        return {'channel': 'TIPS', 'format': 'JSON', 'topic': tips_message.TOPICS[tips_message.BULK],
                'service_code_set': bool(values.get('service_code_bulk')),
                'api_code': tips_setup.api_code(tips_message.BULK) or None, 'govesb_enabled': on,
                'esb_url': (getattr(dj, 'ESB', None) or {}).get('ENGINE_URL') if on else None,
                'request_type': 'NORMAL', 'accounts': len(accounts), 'max_rows_per_message': size,
                'messages': messages, 'skipped_no_bic': no_code, 'withdrawn': 0, 'withdrawn_sample': [],
                'blocked': blocked()}
