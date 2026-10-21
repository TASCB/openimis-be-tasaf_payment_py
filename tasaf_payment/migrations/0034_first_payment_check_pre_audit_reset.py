from django.db import migrations
from django.db.models import Exists, OuterRef

VERIFIED = 1
FINISHED_PAYLISTS = ('CLOSED', 'REJECTED_AT_APPROVAL')


def forwards(apps, schema_editor):
    PaymentAccount = apps.get_model('tasaf_payment', 'PaymentAccount')
    PaylistItem = apps.get_model('tasaf_payment', 'PaylistItem')
    items = PaylistItem.objects.filter(payment_account=OuterRef('pk'), is_deleted=False)
    paid = items.filter(status='PROCESSED')
    in_flight = items.filter(status='PENDING', paylist__is_deleted=False) \
        .exclude(paylist__status__in=FINISHED_PAYLISTS)
    PaymentAccount.objects.filter(is_deleted=False, verification_status=VERIFIED, pre_audit_status='PASSED') \
        .exclude(Exists(paid)).exclude(Exists(in_flight)) \
        .exclude(json_ext__has_key='first_payment_check') \
        .update(pre_audit_status='PENDING')


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0033_gepg_service_code'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
