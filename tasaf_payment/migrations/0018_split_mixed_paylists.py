"""Paylists are BANK or MNO only. Existing MIXED rows (demo seeds) are split: BANK items stay on
the original row, MOBILE items move to a new MNO row. Reverse keeps the split: re-merging batches
is not safe. The schema change is in 0019 (PostgreSQL cannot alter a table with pending trigger
events in the same transaction)."""
import uuid

from django.db import migrations


def split_mixed(apps, schema_editor):
    Paylist = apps.get_model('tasaf_payment', 'Paylist')
    PaylistItem = apps.get_model('tasaf_payment', 'PaylistItem')
    for paylist in Paylist.objects.filter(batch_type='MIXED'):
        items = PaylistItem.objects.filter(paylist_id=paylist.id)
        mobile = items.filter(payment_account__fsp_type='MOBILE')
        if not mobile.exists() or not items.exclude(payment_account__fsp_type='MOBILE').exists():
            paylist.batch_type = 'MNO' if mobile.exists() else 'BANK'
            paylist.save(update_fields=['batch_type'])
            continue
        mobile_ids = list(mobile.values_list('id', flat=True))
        paylist.batch_type = 'BANK'
        paylist.save(update_fields=['batch_type'])
        twin = Paylist.objects.get(id=paylist.id)
        twin.id = uuid.uuid4()
        twin.batch_type = 'MNO'
        twin.batch_group = uuid.uuid4()
        twin.batch_sequence, twin.batch_total = 1, 1
        twin.muse_msg_id = None
        twin.muse_batch_reference = None
        twin.json_ext = {**(twin.json_ext or {}), 'split_from': str(paylist.id)}
        twin.save(force_insert=True)
        PaylistItem.objects.filter(id__in=mobile_ids).update(paylist_id=twin.id)


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0017_muse_sender'),
    ]

    operations = [
        migrations.RunPython(split_mixed, migrations.RunPython.noop),
    ]
