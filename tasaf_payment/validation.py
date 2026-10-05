from django.utils.translation import gettext as _
from django.core.exceptions import ValidationError

from core.validation import BaseModelValidation
from tasaf_payment.models import WithdrawalCharge, PaymentAccount


class PaymentAccountValidation(BaseModelValidation):
    OBJECT_TYPE = PaymentAccount

    @classmethod
    def validate_create(cls, user, **data):
        errors = [
            *validate_required_field(data, 'account_number'),
            *validate_required_field(data, 'fsp_type'),
            *validate_required_field(data, 'fsp_name'),
            *validate_fsp_type(data),
            *validate_mobile_number(data),
        ]
        if errors:
            from django.core.exceptions import ValidationError
            raise ValidationError(errors)
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        existing = PaymentAccount.objects.filter(id=data.get('id'), is_deleted=False).first() if data.get('id') else None
        errors = [
            *validate_fsp_type(data),
            *validate_mobile_number(data, existing),
            *validate_paid_account_lock(data, existing),
        ]
        if errors:
            from django.core.exceptions import ValidationError
            raise ValidationError(errors)
        super().validate_update(user, **data)

    @classmethod
    def validate_delete(cls, user, **data):
        super().validate_delete(user, **data)


def validate_required_field(data, field):
    if not data.get(field):
        return [{"message": _("tasaf_payment.validation.%s_required" % field)}]
    return []


def validate_mobile_number(data, existing=None):
    from tasaf_payment import msisdn
    if existing is not None and 'account_number' not in data and 'fsp_type' not in data:
        return []
    fsp_type = data.get('fsp_type') or getattr(existing, 'fsp_type', None)
    number = data.get('account_number') or getattr(existing, 'account_number', None)
    if fsp_type == 'MOBILE' and number and not msisdn.is_valid(msisdn.normalise(number)):
        return [{"message": _("tasaf_payment.validation.mobile_number_invalid")}]
    return []


def validate_fsp_type(data):
    fsp_type = data.get('fsp_type')
    if fsp_type and fsp_type not in ('BANK', 'MOBILE'):
        return [{"message": _("tasaf_payment.validation.fsp_type_invalid")}]
    return []


class WithdrawalChargeValidation(BaseModelValidation):
    """Bands must be sane and must not overlap: an overlap makes the applicable charge
    ambiguous, and the wrong charge underpays or overpays a beneficiary.
    """
    OBJECT_TYPE = WithdrawalCharge

    @classmethod
    def _check(cls, **data):
        errors = []
        lower, upper = data.get('lower_amount'), data.get('upper_amount')
        if lower is not None and upper is not None and lower > upper:
            errors.append("Lower amount is above upper amount")
        if data.get('withdrawal') is not None and float(data['withdrawal']) < 0:
            errors.append("Withdrawal charge cannot be negative")

        fsp_code = data.get('fsp_code')
        if fsp_code and lower is not None and upper is not None:
            clash = WithdrawalCharge.objects.filter(
                is_deleted=False, fsp_code=fsp_code,
                lower_amount__lte=upper, upper_amount__gte=lower)
            if data.get('id'):
                clash = clash.exclude(id=data['id'])
            if clash.exists():
                errors.append("Band overlaps an existing band for this FSP")
        if errors:
            raise ValidationError(' '.join(errors))
        return []

    @classmethod
    def validate_create(cls, user, **data):
        return cls._check(**data)

    @classmethod
    def validate_update(cls, user, **data):
        return cls._check(**data)

    @classmethod
    def validate_delete(cls, user, **data):
        return []


def validate_paid_account_lock(data, existing):
    if existing is None:
        return []
    from tasaf_payment.services import LOCKED_PAYMENT_FIELDS, account_paid_at
    changed = [f for f in LOCKED_PAYMENT_FIELDS if f in data and data[f] != getattr(existing, f)]
    if changed and account_paid_at(existing.id):
        return [{'message': f"This account has been paid, so {', '.join(changed)} can only be changed through "
                            "Case Management > Correct payment details (reason and approval)."}]
    return []
