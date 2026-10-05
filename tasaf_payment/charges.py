"""Withdrawal-charge resolution and tariff-table maintenance. for the rationale.
"""
import csv
import io
import re
import uuid
from datetime import date
from decimal import Decimal

from django.db.models import Q

from tasaf_payment.models import WithdrawalCharge


class ChargeError(Exception):
    """Raised when a charge cannot be resolved. Never swallow this into a zero charge."""

    def __init__(self, code, message, **ctx):
        super().__init__(message)
        self.code = code
        self.message = message
        self.ctx = ctx


NO_BAND = 'NO_BAND_CONFIGURED'
NO_FSP_CODE = 'FSP_CODE_UNMAPPED'


def normalise_fsp(value):
    """Uppercase and strip everything that is not a letter or digit."""
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def resolve_fsp_code(fsp_name, aliases=None):
    """Map a PaymentAccount.fsp_name to a tariff-table fsp_code.

    The two vocabularies genuinely differ -- accounts hold display names
    ('Vodacom M-Pesa'), the tariff table holds codes ('MPESA') -- and no amount of
    normalisation bridges that, so the alias map is authoritative and configurable.
    """
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.models import FspMapping
    key = normalise_fsp(fsp_name)
    if not key:
        raise ChargeError(NO_FSP_CODE, "Payment account has no FSP name", fsp_name=fsp_name)

    # UI-managed mappings win, so a new FSP can be onboarded without a config change.
    mapped = (FspMapping.objects.filter(is_deleted=False, fsp_name_key=key)
              .values_list('fsp_code', flat=True).first())
    if mapped:
        return mapped

    aliases = aliases if aliases is not None else (TasafPaymentConfig.fsp_code_aliases or {})
    normalised_aliases = {normalise_fsp(k): normalise_fsp(v) for k, v in aliases.items()}
    return normalised_aliases.get(key, key)


def seed_fsp_mappings(user):
    """Materialise the config aliases as editable rows, once. Existing rows are left alone."""
    from tasaf_payment.apps import TasafPaymentConfig
    from tasaf_payment.models import FspMapping
    created = 0
    for name, code in (TasafPaymentConfig.fsp_code_aliases or {}).items():
        key = normalise_fsp(name)
        if FspMapping.objects.filter(is_deleted=False, fsp_name_key=key).exists():
            continue
        FspMapping(fsp_name=name, fsp_code=code).save(username=user.username)
        created += 1
    return created


def lookup_charge(fsp_code, net_amount, on_date=None):
    """Charge for a NET amount. Banding on the net terminates; banding on the gross is
    circular because adding the charge can push the amount into a higher band.

    Returns Decimal (0 is a valid configured charge). Raises ChargeError when no band
    covers the amount -- an unconfigured gap must never be silently treated as zero.
    """
    on_date = on_date or date.today()
    net = Decimal(str(net_amount))
    band = (
        WithdrawalCharge.objects.filter(
            is_deleted=False,
            fsp_code=fsp_code,
            lower_amount__lte=net,
            upper_amount__gte=net,
        )
        .filter(Q(effective_from__isnull=True) | Q(effective_from__lte=on_date))
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .order_by('-effective_from')
        .first()
    )
    if band is None:
        raise ChargeError(
            NO_BAND,
            f"No withdrawal-charge band configured for {fsp_code} at {net}",
            fsp_code=fsp_code, net_amount=str(net), on_date=str(on_date),
        )
    return band.withdrawal


def gross_up(fsp_name, net_amount, on_date=None, aliases=None):
    """(net, charge, gross) for one line. gross is what MUSE is asked to move."""
    net = Decimal(str(net_amount or 0))
    code = resolve_fsp_code(fsp_name, aliases)
    charge = lookup_charge(code, net, on_date)
    return net, charge, net + charge


def coverage(fsp_code, on_date=None):
    """Configured vs unconfigured ranges for one FSP, so gaps are filled deliberately
    rather than discovered when a payment underpays."""
    on_date = on_date or date.today()
    bands = list(
        WithdrawalCharge.objects.filter(is_deleted=False, fsp_code=fsp_code)
        .filter(Q(effective_from__isnull=True) | Q(effective_from__lte=on_date))
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .order_by('lower_amount')
        .values_list('lower_amount', 'upper_amount')
    )
    gaps, overlaps = [], []
    if not bands:
        return {'fsp_code': fsp_code, 'bands': 0, 'lowest': None, 'highest': None,
                'gaps': [], 'overlaps': [], 'covers_from_zero': False}
    for prev, cur in zip(bands, bands[1:]):
        if cur[0] > prev[1] + 1:
            gaps.append({'from': str(prev[1] + 1), 'to': str(cur[0] - 1)})
        elif cur[0] <= prev[1]:
            overlaps.append({'from': str(cur[0]), 'to': str(prev[1])})
    return {
        'fsp_code': fsp_code,
        'bands': len(bands),
        'lowest': str(bands[0][0]),
        'highest': str(bands[-1][1]),
        'gaps': gaps,
        'overlaps': overlaps,
        'covers_from_zero': bands[0][0] <= 1,
    }


def import_rows(rows, user, effective_from=None, replace=False):
    """Load tariff rows. Validates rather than assuming: blank bounds and overlaps are
    rejected, gaps are reported for configuration but do not block the import.

    rows: dicts with EPAYMENT_CODE, LOWER_AMOUNT, UPPER_AMOUNT, WITHDRAWAL.
    """
    created, errors = [], []
    for idx, raw in enumerate(rows, start=2):
        code = (raw.get('EPAYMENT_CODE') or '').strip()
        lo, hi, wd = (str(raw.get(k) or '').strip()
                      for k in ('LOWER_AMOUNT', 'UPPER_AMOUNT', 'WITHDRAWAL'))
        if not code:
            errors.append({'line': idx, 'error': 'missing EPAYMENT_CODE'})
            continue
        if lo == '' or hi == '':
            errors.append({'line': idx, 'error': 'blank LOWER_AMOUNT/UPPER_AMOUNT', 'fsp': code})
            continue
        try:
            lo_d, hi_d, wd_d = Decimal(lo), Decimal(hi), Decimal(wd or '0')
        except Exception:
            errors.append({'line': idx, 'error': 'non-numeric amount', 'fsp': code})
            continue
        if lo_d > hi_d:
            errors.append({'line': idx, 'error': 'LOWER_AMOUNT above UPPER_AMOUNT', 'fsp': code})
            continue
        created.append(WithdrawalCharge(
            id=uuid.uuid4(), fsp_code=normalise_fsp(code),
            lower_amount=lo_d, upper_amount=hi_d, withdrawal=wd_d,
            effective_from=effective_from, is_deleted=False, version=1,
            user_created=user, user_updated=user, json_ext={},
        ))

    if replace:
        WithdrawalCharge.objects.filter(is_deleted=False).update(is_deleted=True)
    WithdrawalCharge.objects.bulk_create(created, batch_size=500)

    codes = sorted({c.fsp_code for c in created})
    return {
        'imported': len(created),
        'skipped': len(errors),
        'errors': errors,
        'coverage': [coverage(c) for c in codes],
    }


def import_csv(content, user, effective_from=None, replace=False):
    text = content.decode('utf-8-sig') if isinstance(content, bytes) else content
    return import_rows(list(csv.DictReader(io.StringIO(text))), user,
                       effective_from=effective_from, replace=replace)


def known_fsps():
    """FSP options for the UI: every distinct fsp_name on payment accounts, plus every code
    already present in the tariff table. Typing a code by hand is how a mapping silently
    fails to match, so the UI picks from this list instead.
    """
    from tasaf_payment.models import FspMapping, PaymentAccount
    out = {}
    for name in (PaymentAccount.objects.filter(is_deleted=False)
                 .values_list('fsp_name', flat=True).distinct()):
        if not name:
            continue
        key = normalise_fsp(name)
        code = (FspMapping.objects.filter(is_deleted=False, fsp_name_key=key)
                .values_list('fsp_code', flat=True).first()) or key
        out.setdefault(code, {'fsp_code': code, 'fsp_name': name, 'on_accounts': True,
                              'has_bands': False})
    for code in (WithdrawalCharge.objects.filter(is_deleted=False)
                 .values_list('fsp_code', flat=True).distinct()):
        row = out.setdefault(code, {'fsp_code': code, 'fsp_name': code,
                                    'on_accounts': False, 'has_bands': False})
        row['has_bands'] = True
    return sorted(out.values(), key=lambda r: r['fsp_code'])


FSP_CHARGES_EVENT = 'tasaf_payment.fsp_charges.update'


def validate_band_set(bands):
    """Validate a whole FSP band set before it is proposed. Overlaps make the applicable
    charge ambiguous, so they are rejected here rather than discovered at payment time."""
    errors = []
    rows = []
    for i, b in enumerate(bands, start=1):
        try:
            lo, hi = Decimal(str(b['lower_amount'])), Decimal(str(b['upper_amount']))
            wd = Decimal(str(b.get('withdrawal') or 0))
        except Exception:
            errors.append(f"Row {i}: amounts must be numeric")
            continue
        if lo > hi:
            errors.append(f"Row {i}: lower amount is above upper amount")
        if wd < 0:
            errors.append(f"Row {i}: charge cannot be negative")
        rows.append((lo, hi, wd))
    rows.sort()
    for (a_lo, a_hi, _), (b_lo, b_hi, _) in zip(rows, rows[1:]):
        if b_lo <= a_hi:
            errors.append(f"Bands overlap: {a_lo}-{a_hi} and {b_lo}-{b_hi}")
    return errors


def apply_band_set(fsp_code, bands, user, effective_from=None):
    """Replace every band for one FSP in a single operation, so the set is always
    internally consistent -- a half-applied tariff would silently misprice payments."""
    from django.db import transaction
    code = normalise_fsp(fsp_code)
    with transaction.atomic():
        WithdrawalCharge.objects.filter(is_deleted=False, fsp_code=code).update(is_deleted=True)
        objs = [WithdrawalCharge(
            id=uuid.uuid4(), fsp_code=code,
            lower_amount=Decimal(str(b['lower_amount'])),
            upper_amount=Decimal(str(b['upper_amount'])),
            withdrawal=Decimal(str(b.get('withdrawal') or 0)),
            effective_from=effective_from or b.get('effective_from'),
            is_deleted=False, version=1,
            user_created=user, user_updated=user, json_ext={},
        ) for b in bands]
        WithdrawalCharge.objects.bulk_create(objs, batch_size=500)
    return len(objs)


def band_set(fsp_code):
    """Current bands for one FSP, for the config editor."""
    return list(
        WithdrawalCharge.objects.filter(is_deleted=False, fsp_code=normalise_fsp(fsp_code))
        .order_by('lower_amount')
        .values('lower_amount', 'upper_amount', 'withdrawal', 'effective_from')
    )
