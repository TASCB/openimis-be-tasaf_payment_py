"""Drop the MIXED batch type and the unused Paylist.location."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0018_split_mixed_paylists'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='historicalpaylist',
            name='location',
        ),
        migrations.RemoveField(
            model_name='paylist',
            name='location',
        ),
        migrations.AlterField(
            model_name='historicalpaylist',
            name='batch_type',
            field=models.CharField(choices=[('BANK', 'BANK'), ('MNO', 'MNO')], max_length=10),
        ),
        migrations.AlterField(
            model_name='paylist',
            name='batch_type',
            field=models.CharField(choices=[('BANK', 'BANK'), ('MNO', 'MNO')], max_length=10),
        ),
    ]
