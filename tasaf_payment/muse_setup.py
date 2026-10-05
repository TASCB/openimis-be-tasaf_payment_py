"""MUSE set-up data: per-FSP routing (bank name, channel, BIC) and the accounting values MUSE
supplies. Every change is proposed, then applied only when someone other than the requester
approves it in the approval engine."""
import logging
import re
from datetime import datetime

from django.db import transaction
from django.db.models import Count

logger = logging.getLogger(__name__)

BIC_RE = re.compile(r'^[A-Z]{4}TZT[ZX0]$')
FSP_TYPES = ('BANK', 'MOBILE')
CURRENCY = 'TZS'
DOMAIN = 'tasaf_payment.MuseChangeRequest'


class SetupError(ValueError):
    pass


def main_fsp_name(row, bank_name=''):
    if row['usage']:
        return max(row['usage'], key=row['usage'].get)
    return (sorted(row['names']) or [bank_name or row['fsp_code']])[0]


def provider_list():
    """One row per FSP code: its MUSE routing data, the account display names that map to
    it, whether it has charge bands, how many accounts use it, and what is still missing."""
    from tasaf_payment.charges import known_fsps, normalise_fsp, resolve_fsp_code
    from tasaf_payment.models import FspMapping, FspProfile, PaymentAccount, WithdrawalCharge

    rows = {r['fsp_code']: {'fsp_code': r['fsp_code'], 'names': [], 'has_bands': r['has_bands'],
                            'accounts': 0, 'account_types': set(), 'usage': {}} for r in known_fsps()}
    for m in FspMapping.objects.filter(is_deleted=False):
        rows.setdefault(m.fsp_code, {'fsp_code': m.fsp_code, 'names': [], 'has_bands': False,
                                     'accounts': 0, 'account_types': set(), 'usage': {}})
        rows[m.fsp_code]['names'].append(m.fsp_name)
    usage = (PaymentAccount.objects.filter(is_deleted=False)
             .values('fsp_name', 'fsp_type').annotate(n=Count('id')))
    for u in usage:
        if not u['fsp_name']:
            continue
        code = resolve_fsp_code(u['fsp_name']) or normalise_fsp(u['fsp_name'])
        row = rows.setdefault(code, {'fsp_code': code, 'names': [], 'has_bands': False,
                                     'accounts': 0, 'account_types': set(), 'usage': {}})
        row['accounts'] += u['n']
        row['usage'][u['fsp_name']] = row['usage'].get(u['fsp_name'], 0) + u['n']
        if u['fsp_type']:
            row['account_types'].add(u['fsp_type'])
        if u['fsp_name'] not in row['names']:
            row['names'].append(u['fsp_name'])

    profiles = {p.fsp_code: p for p in FspProfile.objects.filter(is_deleted=False)}
    band_counts = dict(WithdrawalCharge.objects.filter(is_deleted=False).values('fsp_code')
                       .annotate(n=Count('id')).values_list('fsp_code', 'n'))
    pending = set(pending_fsp_codes())
    out = []
    for code, row in sorted(rows.items()):
        p = profiles.get(code)
        # The channel defaults to what the accounts say, so an unset profile is still useful.
        inferred = next(iter(row['account_types'])) if len(row['account_types']) == 1 else ''
        fsp_type = (p.fsp_type if p and p.fsp_type else inferred)
        bank_name = p.bank_name if p else ''
        bic = p.bic if p else ''
        missing = [f for f, v in (('bankName', bank_name), ('bic', bic), ('fspType', fsp_type)) if not v]
        out.append({
            'uuid': str(p.uuid) if p else None,
            'fsp_code': code, 'name': main_fsp_name(row, bank_name), 'band_count': band_counts.get(code, 0),
            'bank_name': bank_name, 'fsp_type': fsp_type, 'bic': bic,
            'names': sorted(row['names']), 'has_bands': row['has_bands'],
            'accounts': row['accounts'], 'missing': missing, 'pending': code in pending,
        })
    return out



def normalise_profile(fsp_code, bank_name, fsp_type, bic):
    from tasaf_payment.charges import normalise_fsp

    code = normalise_fsp(fsp_code or '')
    values = {'bank_name': (bank_name or '').strip(), 'fsp_type': (fsp_type or '').strip().upper(),
              'bic': (bic or '').strip().upper()}
    errors = []
    if not code:
        errors.append('FSP code is required')
    if not values['bank_name']:
        errors.append('Bank / FSP name for MUSE is required')
    if values['fsp_type'] not in FSP_TYPES:
        errors.append('Channel must be BANK or MOBILE')
    if not BIC_RE.match(values['bic']):
        errors.append('BIC must be 8 characters in the Tanzanian form '
                      '(four letters, then TZT, then Z, X or 0)')
    if errors:
        raise SetupError('; '.join(errors))
    return code, values


def profile_values(fsp_code):
    from tasaf_payment.models import FspProfile
    p = FspProfile.objects.filter(fsp_code=fsp_code, is_deleted=False).first()
    return {'bank_name': p.bank_name, 'fsp_type': p.fsp_type, 'bic': p.bic} if p else {}


SETTINGS_FIELDS = ('institution_code', 'payer_account', 'sub_budget_class',
                   'unapplied_sub_budget_class', 'payment_desc', 'is_stp',
                   'gl_accounts')
REQUIRED_SETTINGS = ('institution_code', 'payer_account', 'sub_budget_class', 'payment_desc')


def get_settings():
    from tasaf_payment.models import MuseSettings
    return MuseSettings.objects.filter(is_deleted=False).order_by('-date_created').first()


def _int_or_none(value, label, errors):
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f'{label} must be a whole number')
        return None


def normalise_settings(data):
    errors = []
    gl = data.get('gl_accounts') or []
    if not isinstance(gl, list):
        errors.append('GL accounts must be a list')
        gl = []
    for i, line in enumerate(gl, start=1):
        if not isinstance(line, dict) or not (line.get('glaccount') or '').strip():
            errors.append(f'GL line {i}: the GL account code is required')
    values = {
        'institution_code': (data.get('institution_code') or '').strip(),
        'payer_account': (data.get('payer_account') or '').strip(),
        'sub_budget_class': _int_or_none(data.get('sub_budget_class'), 'Sub-budget class', errors),
        'unapplied_sub_budget_class': _int_or_none(data.get('unapplied_sub_budget_class'),
                                                   'Sub-budget class (unapplied)', errors),
        'payment_desc': (data.get('payment_desc') or '').strip(),
        'is_stp': bool(data.get('is_stp')),
        'gl_accounts': [
            {'glaccount': line['glaccount'].strip(),
             'glaccountDesc': (line.get('glaccountDesc') or '').strip(),
             'grantName': (line.get('grantName') or '').strip()}
            for line in gl if isinstance(line, dict) and (line.get('glaccount') or '').strip()
        ],
    }
    if errors:
        raise SetupError('; '.join(errors))
    return values


def settings_values():
    s = get_settings()
    return {f: getattr(s, f) for f in SETTINGS_FIELDS} if s else {}


def server_environment():
    from django.conf import settings
    return (getattr(settings, 'MUSE_ENVIRONMENT', '') or '').strip().upper()


# ── Change requests ─────────────────────────────────────────────────────────

def pending_fsp_codes():
    from tasaf_payment.models import MuseChangeKind, MuseChangeRequest, MuseChangeStatus
    return (MuseChangeRequest.objects
            .filter(is_deleted=False, kind=MuseChangeKind.FSP_PROFILE, status=MuseChangeStatus.PENDING)
            .values_list('fsp_code', flat=True))


def propose(user, kind, proposed, fsp_code=''):
    """Record a change and open its approval. Applies nothing."""
    from approval.services import ApprovalService
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.models import MuseChangeKind, MuseChangeRequest, MuseChangeStatus

    current = settings_values() if kind == MuseChangeKind.SETTINGS else profile_values(fsp_code)
    if current and all(current.get(k) == v for k, v in proposed.items()):
        raise SetupError('Nothing was changed')
    with transaction.atomic():
        if MuseChangeRequest.objects.select_for_update().filter(
                is_deleted=False, kind=kind, fsp_code=fsp_code,
                status=MuseChangeStatus.PENDING).exists():
            raise SetupError('A change is already awaiting approval; approve, reject or cancel it first')
        change = MuseChangeRequest(kind=kind, fsp_code=fsp_code, proposed=proposed, current=current)
        change.save(username=user.username)
        result = ApprovalService(user).request_approval(
            change, TasafPaymentConfig.muse_change_approval_flow,
            summary={'kind': kind, 'fsp_code': fsp_code,
                     'fields': sorted(k for k, v in proposed.items() if current.get(k) != v)})
        if not result.get('success'):
            raise SetupError(result.get('detail') or result.get('message') or 'Approval could not be opened')
    return change


def propose_settings(user, data):
    from tasaf_payment.models import MuseChangeKind
    return propose(user, MuseChangeKind.SETTINGS, normalise_settings(data))


def delete_fsp(user, fsp_code):
    from tasaf_payment.charges import normalise_fsp
    from tasaf_payment.models import (
        FspMapping, FspProfile, MuseChangeKind, MuseChangeRequest, MuseChangeStatus, WithdrawalCharge,
    )

    code = normalise_fsp(fsp_code or '')
    row = next((r for r in provider_list() if r['fsp_code'] == code), None)
    if row is None:
        raise SetupError(f'No FSP {code}')
    if row['accounts']:
        raise SetupError(f'{code} is used by {row["accounts"]} payment account(s) and cannot be deleted')
    if MuseChangeRequest.objects.filter(is_deleted=False, kind=MuseChangeKind.FSP_PROFILE, fsp_code=code,
                                        status=MuseChangeStatus.PENDING).exists():
        raise SetupError(f'{code} has a MUSE change awaiting approval; decide it first')
    with transaction.atomic():
        for model in (FspMapping, WithdrawalCharge, FspProfile):
            for obj in model.objects.filter(is_deleted=False, fsp_code=code):
                obj.is_deleted = True
                obj.save(username=user.username)
    return code


def propose_profile(user, fsp_code, bank_name, fsp_type, bic):
    from tasaf_payment.models import MuseChangeKind
    code, values = normalise_profile(fsp_code, bank_name, fsp_type, bic)
    return propose(user, MuseChangeKind.FSP_PROFILE, values, fsp_code=code)


def _apply(change, user):
    from tasaf_payment.models import FspProfile, MuseChangeKind, MuseSettings

    values = change.proposed
    if change.kind == MuseChangeKind.SETTINGS:
        target = get_settings() or MuseSettings()
        for field in SETTINGS_FIELDS:
            if field in values:
                setattr(target, field, values[field])
        target.environment = server_environment()
    else:
        target = (FspProfile.objects.filter(fsp_code=change.fsp_code, is_deleted=False).first()
                  or FspProfile(fsp_code=change.fsp_code))
        target.bank_name, target.fsp_type, target.bic = values['bank_name'], values['fsp_type'], values['bic']
    target.save(username=user.username)


def _engine_request(change):
    from approval.models import ApprovalRequest, RequestStatus
    return ApprovalRequest.objects.filter(
        object_id=str(change.id), status=RequestStatus.PENDING, is_deleted=False,
    ).order_by('-date_created').first()


def _pending(change_id):
    from tasaf_payment.models import MuseChangeRequest, MuseChangeStatus
    change = MuseChangeRequest.objects.filter(id=change_id, is_deleted=False).first()
    if not change:
        raise SetupError('Change request not found')
    if change.status != MuseChangeStatus.PENDING:
        raise SetupError('This change is no longer awaiting approval')
    appr = _engine_request(change)
    if not appr:
        raise SetupError('No open approval for this change')
    return change, appr


def decide(user, change_id, approve, comment=None):
    """Approve or reject through the engine; the finalized adapter applies or closes it."""
    from approval.services import ApprovalService

    change, appr = _pending(change_id)
    step = appr.steps.filter(order=appr.current_step_order, is_deleted=False).first()
    service = ApprovalService(user)
    result = (service.approve if approve else service.reject)(str(appr.id), str(step.id), comment=comment)
    if not result.get('success'):
        raise SetupError(result.get('message') or result.get('detail') or 'Decision failed')
    change.refresh_from_db()
    return change


def cancel(user, change_id, reason=None):
    from approval.services import ApprovalService

    change, appr = _pending(change_id)
    result = ApprovalService(user).cancel(str(appr.id), reason=reason)
    if not result.get('success'):
        raise SetupError(result.get('message') or result.get('detail') or 'Cancel failed')
    change.refresh_from_db()
    return change


def on_approval_finalized(**kwargs):
    """Engine adapter: apply an approved change, close a rejected or cancelled one."""
    try:
        data = (kwargs.get('result') or {}).get('data') or {}
        if data.get('domain') != DOMAIN:
            return
        from approval.models import ApprovalRequest
        from tasaf_payment.models import MuseChangeRequest, MuseChangeStatus

        appr = ApprovalRequest.objects.filter(id=data.get('id')).first()
        change = appr and MuseChangeRequest.objects.filter(id=appr.object_id, is_deleted=False).first()
        if not change or change.status != MuseChangeStatus.PENDING:
            return
        user = getattr(kwargs.get('cls_'), 'user', None)
        decision = data.get('decision')
        if decision == 'APPROVED':
            _apply(change, user)
            change.status = MuseChangeStatus.APPROVED
        elif decision in ('REJECTED', 'CANCELLED'):
            change.status = decision
        else:
            return
        change.decided_at = datetime.now()
        change.save(username=user.username)
        logger.info("MUSE change %s %s by %s", change.id, change.status, user.username)
    except Exception as exc:
        logger.error("tasaf_payment MUSE change adapter failed", exc_info=exc)
        return [str(exc)]


def change_rows(status=None, limit=50):
    from approval.models import ApprovalDecision
    from tasaf_payment.models import MuseChangeRequest

    qs = MuseChangeRequest.objects.filter(is_deleted=False).select_related('user_created')
    if status:
        qs = qs.filter(status=status)
    rows = []
    for c in qs.order_by('-date_created')[:limit]:
        decision = (ApprovalDecision.objects
                    .filter(step__approval_request__object_id=str(c.id), is_deleted=False)
                    .select_related('approver').order_by('-date_created').first())
        rows.append({
            'id': str(c.id), 'kind': c.kind, 'fsp_code': c.fsp_code, 'status': c.status,
            'proposed': c.proposed, 'current': c.current,
            'changed': sorted(k for k, v in c.proposed.items() if (c.current or {}).get(k) != v),
            'requested_by': getattr(c.user_created, 'username', None),
            'requested_at': c.date_created.isoformat() if c.date_created else None,
            'decided_by': getattr(getattr(decision, 'approver', None), 'username', None),
            'decided_at': c.decided_at.isoformat() if c.decided_at else None,
            'comment': getattr(decision, 'comment', None),
        })
    return rows


def readiness():
    """What still stops a MUSE payment message from being built: settings not yet provided
    by MUSE, and FSPs used on accounts without a BIC / bank name / channel."""
    s = get_settings()
    settings_missing = [f for f in REQUIRED_SETTINGS if (getattr(s, f) if s else None) in (None, '')]
    providers_missing = [p['fsp_code'] for p in provider_list() if p['accounts'] and p['missing']]
    server = server_environment()
    approved_on = s.environment if s else ''
    environment_mismatch = bool(s) and approved_on != server
    return {'settings_missing': settings_missing, 'providers_missing': providers_missing,
            'server_environment': server, 'settings_environment': approved_on,
            'environment_mismatch': environment_mismatch,
            'ready': not settings_missing and not providers_missing and not environment_mismatch}
