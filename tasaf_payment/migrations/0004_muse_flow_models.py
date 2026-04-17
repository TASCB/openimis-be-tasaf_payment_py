"""
Migration 0004: MUSE verification flow models.

Changes:
  PaymentAccount
    - Add muse_verification_reference
    - Add pre_audit_status
    - Add active_check_status
    - Add index on pre_audit_status
    - Rename indexes to explicit names (idempotent — AddIndex with name is safe)

  New models:
    - MuseVerificationRecord  (replaces NIDA VerificationRecord for new runs)
    - Paylist
    - PaylistItem
    - ReturnFeedback

  VerificationRecord is kept unchanged (legacy audit trail).
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0003_paymentaccount_guard_index'),
        ('payroll', '0001_initial'),
        ('payment_cycle', '0001_initial'),
        ('location', '0001_initial'),
    ]

    operations = [
        # ------------------------------------------------------------------
        # PaymentAccount — new fields
        # ------------------------------------------------------------------
        migrations.AddField(
            model_name='paymentaccount',
            name='muse_verification_reference',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='paymentaccount',
            name='pre_audit_status',
            field=models.CharField(
                choices=[('PENDING', 'PENDING'), ('PASSED', 'PASSED'), ('FAILED', 'FAILED')],
                default='PENDING',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='paymentaccount',
            name='active_check_status',
            field=models.CharField(
                choices=[('PENDING', 'PENDING'), ('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')],
                default='PENDING',
                max_length=20,
            ),
        ),
        # Add PENDING_MUSE=4 to verification_status choices (DB stores int — no schema change needed)

        # ------------------------------------------------------------------
        # PaymentAccount — new index on pre_audit_status
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name='paymentaccount',
            index=models.Index(fields=['pre_audit_status'], name='tasaf_pa_preaudit_idx'),
        ),

        # ------------------------------------------------------------------
        # MuseVerificationRecord
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name='MuseVerificationRecord',
            fields=[
                ('id', models.AutoField(db_column='ID', primary_key=True, serialize=False)),
                ('uuid', models.CharField(db_column='UUID', default=uuid.uuid4, max_length=36, unique=True)),
                ('date_created', models.DateTimeField(auto_now_add=True, db_column='DateCreated')),
                ('date_updated', models.DateTimeField(auto_now=True, db_column='DateUpdated')),
                ('is_deleted', models.BooleanField(db_column='isDeleted', default=False)),
                ('version', models.IntegerField(db_column='Version', default=1)),
                ('muse_reference', models.CharField(blank=True, max_length=100, null=True)),
                ('verification_type', models.CharField(
                    choices=[
                        ('MOBILE_VALIDATION', 'MOBILE_VALIDATION'),
                        ('FSP_ACCOUNT', 'FSP_ACCOUNT'),
                        ('ACTIVE_CHECK', 'ACTIVE_CHECK'),
                    ],
                    default='FSP_ACCOUNT',
                    max_length=30,
                )),
                ('result', models.CharField(
                    choices=[('PASSED', 'PASSED'), ('FAILED', 'FAILED'), ('MANUAL', 'MANUAL')],
                    max_length=10,
                )),
                ('failure_reason', models.TextField(blank=True, null=True)),
                ('raw_response', models.JSONField(blank=True, default=dict)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('payment_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='muse_verification_records',
                    to='tasaf_payment.paymentaccount',
                )),
            ],
            options={
                'db_table': 'tasaf_MuseVerificationRecord',
                'managed': True,
            },
        ),
        migrations.AddIndex(
            model_name='museverificationrecord',
            index=models.Index(
                fields=['payment_account', 'received_at'],
                name='tasaf_muse_vr_account_idx',
            ),
        ),

        # ------------------------------------------------------------------
        # Paylist
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name='Paylist',
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
                ('batch_type', models.CharField(
                    choices=[('BANK', 'BANK'), ('MNO', 'MNO'), ('MIXED', 'MIXED')],
                    max_length=10,
                )),
                ('status', models.CharField(
                    choices=[
                        ('DRAFT', 'DRAFT'),
                        ('PENDING_APPROVAL', 'PENDING_APPROVAL'),
                        ('APPROVED', 'APPROVED'),
                        ('SUBMITTED', 'SUBMITTED'),
                        ('CLOSED', 'CLOSED'),
                    ],
                    default='DRAFT',
                    max_length=20,
                )),
                ('generated_at', models.DateTimeField(blank=True, null=True)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('submitted_at', models.DateTimeField(blank=True, null=True)),
                ('muse_batch_reference', models.CharField(blank=True, max_length=100, null=True)),
                ('payroll', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='paylists',
                    to='payroll.payroll',
                )),
                ('payment_cycle', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='paylists',
                    to='payment_cycle.paymentcycle',
                )),
                ('location', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='paylists',
                    to='location.location',
                )),
            ],
            options={
                'db_table': 'tasaf_Paylist',
                'managed': True,
            },
        ),
        migrations.AddIndex(
            model_name='paylist',
            index=models.Index(fields=['status'], name='tasaf_paylist_status_idx'),
        ),
        migrations.AddIndex(
            model_name='paylist',
            index=models.Index(fields=['batch_type'], name='tasaf_paylist_btype_idx'),
        ),

        # ------------------------------------------------------------------
        # PaylistItem
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name='PaylistItem',
            fields=[
                ('id', models.AutoField(db_column='ID', primary_key=True, serialize=False)),
                ('uuid', models.CharField(db_column='UUID', default=uuid.uuid4, max_length=36, unique=True)),
                ('date_created', models.DateTimeField(auto_now_add=True, db_column='DateCreated')),
                ('date_updated', models.DateTimeField(auto_now=True, db_column='DateUpdated')),
                ('is_deleted', models.BooleanField(db_column='isDeleted', default=False)),
                ('version', models.IntegerField(db_column='Version', default=1)),
                ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ('status', models.CharField(
                    choices=[
                        ('PENDING', 'PENDING'),
                        ('PROCESSED', 'PROCESSED'),
                        ('RETURNED', 'RETURNED'),
                        ('UNAPPLIED', 'UNAPPLIED'),
                    ],
                    default='PENDING',
                    max_length=20,
                )),
                ('muse_reference', models.CharField(blank=True, max_length=100, null=True)),
                ('return_reason', models.TextField(blank=True, null=True)),
                ('final_status', models.CharField(blank=True, max_length=50, null=True)),
                ('paylist', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='items',
                    to='tasaf_payment.paylist',
                )),
                ('payment_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='paylist_items',
                    to='tasaf_payment.paymentaccount',
                )),
                ('benefit_consumption', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='paylist_items',
                    to='payroll.benefitconsumption',
                )),
            ],
            options={
                'db_table': 'tasaf_PaylistItem',
                'managed': True,
            },
        ),
        migrations.AddIndex(
            model_name='paylistitem',
            index=models.Index(fields=['paylist', 'status'], name='tasaf_pli_status_idx'),
        ),

        # ------------------------------------------------------------------
        # ReturnFeedback
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name='ReturnFeedback',
            fields=[
                ('id', models.AutoField(db_column='ID', primary_key=True, serialize=False)),
                ('uuid', models.CharField(db_column='UUID', default=uuid.uuid4, max_length=36, unique=True)),
                ('date_created', models.DateTimeField(auto_now_add=True, db_column='DateCreated')),
                ('date_updated', models.DateTimeField(auto_now=True, db_column='DateUpdated')),
                ('is_deleted', models.BooleanField(db_column='isDeleted', default=False)),
                ('version', models.IntegerField(db_column='Version', default=1)),
                ('feedback_type', models.CharField(
                    choices=[
                        ('UNAPPLIED', 'UNAPPLIED'),
                        ('RETURNED', 'RETURNED'),
                        ('PARTIAL', 'PARTIAL'),
                    ],
                    max_length=20,
                )),
                ('reason_code', models.CharField(blank=True, max_length=50, null=True)),
                ('reason_description', models.TextField(blank=True, null=True)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('paylist_item', models.ForeignKey(
                    on_delete=django.db.models.deletion.DO_NOTHING,
                    related_name='return_feedbacks',
                    to='tasaf_payment.paylistitem',
                )),
            ],
            options={
                'db_table': 'tasaf_ReturnFeedback',
                'managed': True,
            },
        ),
    ]
