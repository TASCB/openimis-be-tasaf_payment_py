import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('core', '0001_initial'),
        ('social_protection', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentAccount',
            fields=[
                ('id', models.AutoField(db_column='ID', primary_key=True, serialize=False)),
                ('uuid', models.CharField(db_column='UUID', default=uuid.uuid4, max_length=36, unique=True)),
                ('date_created', models.DateTimeField(auto_now_add=True, db_column='DateCreated')),
                ('date_updated', models.DateTimeField(auto_now=True, db_column='DateUpdated')),
                ('is_deleted', models.BooleanField(db_column='isDeleted', default=False)),
                ('version', models.IntegerField(db_column='Version', default=1)),
                ('date_valid_from', models.DateTimeField(blank=True, db_column='DateValidFrom', null=True)),
                ('date_valid_to', models.DateTimeField(blank=True, db_column='DateValidTo', null=True)),
                ('json_ext', models.JSONField(blank=True, db_column='Json_ext', default=dict)),
                ('account_number', models.CharField(max_length=50)),
                ('account_name', models.CharField(blank=True, max_length=255, null=True)),
                ('fsp_type', models.CharField(max_length=20)),
                ('fsp_name', models.CharField(max_length=100)),
                ('verification_status', models.IntegerField(
                    choices=[(0, 'PENDING'), (1, 'VERIFIED'), (2, 'FAILED'), (3, 'MANUAL')],
                    default=0,
                )),
                ('verification_score', models.IntegerField(blank=True, null=True)),
                ('is_primary', models.BooleanField(default=True)),
                ('group_beneficiary', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='payment_accounts',
                    to='social_protection.groupbeneficiary',
                )),
            ],
            options={
                'db_table': 'tasaf_PaymentAccount',
                'managed': True,
            },
        ),
        migrations.CreateModel(
            name='VerificationRecord',
            fields=[
                ('id', models.AutoField(db_column='ID', primary_key=True, serialize=False)),
                ('uuid', models.CharField(db_column='UUID', default=uuid.uuid4, max_length=36, unique=True)),
                ('date_created', models.DateTimeField(auto_now_add=True, db_column='DateCreated')),
                ('date_updated', models.DateTimeField(auto_now=True, db_column='DateUpdated')),
                ('is_deleted', models.BooleanField(db_column='isDeleted', default=False)),
                ('version', models.IntegerField(db_column='Version', default=1)),
                ('nida_name', models.CharField(blank=True, max_length=255, null=True)),
                ('fsp_name_returned', models.CharField(blank=True, max_length=255, null=True)),
                ('match_score', models.IntegerField(blank=True, null=True)),
                ('routing_decision', models.CharField(blank=True, max_length=20, null=True)),
                ('run_reference', models.CharField(blank=True, max_length=50, null=True)),
                ('payment_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='verification_records',
                    to='tasaf_payment.paymentaccount',
                )),
            ],
            options={
                'db_table': 'tasaf_VerificationRecord',
                'managed': True,
            },
        ),
        migrations.CreateModel(
            name='PaymentAccountMutation',
            fields=[
                ('id', models.AutoField(db_column='ID', primary_key=True, serialize=False)),
                ('uuid', models.CharField(db_column='UUID', default=uuid.uuid4, max_length=36, unique=True)),
                ('mutation', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='tasaf_payment',
                    to='core.mutationlog',
                )),
                ('payment_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='mutations',
                    to='tasaf_payment.paymentaccount',
                )),
            ],
            options={
                'db_table': 'tasaf_PaymentAccountMutation',
                'managed': True,
            },
        ),
        migrations.AddIndex(
            model_name='paymentaccount',
            index=models.Index(fields=['account_number'], name='tasaf_paymentaccount_accno_idx'),
        ),
        migrations.AddIndex(
            model_name='paymentaccount',
            index=models.Index(fields=['verification_status'], name='tasaf_paymentaccount_vstatus_idx'),
        ),
        migrations.AddIndex(
            model_name='paymentaccount',
            index=models.Index(fields=['fsp_type', 'fsp_name'], name='tasaf_paymentaccount_fsp_idx'),
        ),
    ]
