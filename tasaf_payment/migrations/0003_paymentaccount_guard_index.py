"""
Add composite index on PaymentAccount(group_beneficiary_id, verification_status,
is_primary, is_deleted).

This index is required for the payroll-creation guard signal to be efficient
at scale.  The guard runs an EXISTS subquery:

    PaymentAccount.objects.filter(
        group_beneficiary=OuterRef('pk'),
        verification_status=VERIFIED,
        is_primary=True,
        is_deleted=False,
    )

Against millions of records this needs to hit an index.  Without it the query
would perform a full table scan on every payroll creation.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0002_add_rights_to_admin'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='paymentaccount',
            index=models.Index(
                fields=['group_beneficiary', 'verification_status', 'is_primary', 'is_deleted'],
                name='tasaf_pa_guard_idx',
            ),
        ),
    ]
