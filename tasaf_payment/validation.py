from django.utils.translation import gettext as _

from core.validation import BaseModelValidation
from tasaf_payment.models import PaymentAccount


class PaymentAccountValidation(BaseModelValidation):
    OBJECT_TYPE = PaymentAccount

    @classmethod
    def validate_create(cls, user, **data):
        errors = [
            *validate_required_field(data, 'account_number'),
            *validate_required_field(data, 'fsp_type'),
            *validate_required_field(data, 'fsp_name'),
            *validate_fsp_type(data),
        ]
        if errors:
            from django.core.exceptions import ValidationError
            raise ValidationError(errors)
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        errors = [
            *validate_fsp_type(data),
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


def validate_fsp_type(data):
    fsp_type = data.get('fsp_type')
    if fsp_type and fsp_type not in ('BANK', 'MOBILE'):
        return [{"message": _("tasaf_payment.validation.fsp_type_invalid")}]
    return []
