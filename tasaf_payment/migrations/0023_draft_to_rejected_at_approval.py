"""DRAFT only ever meant "payment approval rejected or cancelled" (generation starts at
PENDING_APPROVAL), and nothing moved it on. Those rows become REJECTED_AT_APPROVAL, a final status
whose benefits may be generated again. Schema change in 0024."""
from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model('tasaf_payment', 'Paylist').objects.filter(status='DRAFT').update(status='REJECTED_AT_APPROVAL')


def backward(apps, schema_editor):
    apps.get_model('tasaf_payment', 'Paylist').objects.filter(status='REJECTED_AT_APPROVAL').update(status='DRAFT')


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0022_drop_currency_code'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
