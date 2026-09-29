"""MUSE reports a failed payment only as UNAPPLIED (a bank return included), so RETURNED items
and RETURNED / PARTIAL feedback become UNAPPLIED. Forward-only. Schema change in 0021."""
from django.db import migrations


def to_unapplied(apps, schema_editor):
    apps.get_model('tasaf_payment', 'PaylistItem').objects.filter(status='RETURNED').update(status='UNAPPLIED')
    apps.get_model('tasaf_payment', 'ReturnFeedback').objects.filter(
        feedback_type__in=('RETURNED', 'PARTIAL')).update(feedback_type='UNAPPLIED')


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0019_bank_mno_only'),
    ]

    operations = [
        migrations.RunPython(to_unapplied, migrations.RunPython.noop),
    ]
