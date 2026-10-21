from django.db import migrations, models

PREFIXES = (
    ('MPESA', ['074', '075', '076', '079']),
    ('TIGOPESA', ['065', '067', '071', '077']),
    ('AIRTELMONEY', ['068', '069', '078']),
    ('HALOPESA', ['061', '062']),
    ('TTCL', ['073']),
)


def seed(apps, schema_editor):
    FspProfile = apps.get_model('tasaf_payment', 'FspProfile')
    for code, prefixes in PREFIXES:
        FspProfile.objects.filter(fsp_code=code, is_deleted=False, mobile_prefixes=[]).update(
            mobile_prefixes=prefixes)


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0034_first_payment_check_pre_audit_reset'),
    ]

    operations = [
        migrations.AddField(
            model_name='fspprofile',
            name='mobile_prefixes',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='historicalfspprofile',
            name='mobile_prefixes',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
