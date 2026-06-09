"""
Grant the tasaf_payment rights to the 'IMIS Administrator' role.

Consolidated from the original hand-written 0002 + 0005 migrations (which were
removed when the initial migration was regenerated to match the installed core
HistoryBusinessModel schema). Idempotent via get_or_create.
"""
from django.db import migrations

RIGHTS = [
    # payment accounts
    152001,  # search payment accounts
    152002,  # create payment account
    152003,  # update payment account
    152004,  # delete payment account
    # verification / approval
    152101,  # run verification
    152102,  # approve payment accounts
    152103,  # generate payroll
    152104,  # submit payroll to MUSE
    152105,  # resubmit failed accounts
    # pre-audit
    152201,  # run pre-audit
    # paylists
    152301,  # search paylists
    152302,  # generate paylist
    152303,  # approve paylist
    152304,  # submit paylist
    # feedback / dashboard / records
    152401,  # return feedback search
    152501,  # dashboard
    152601,  # muse verification records search
]


def add_rights_to_admin(apps, schema_editor):
    Role = apps.get_model('core', 'Role')
    RoleRight = apps.get_model('core', 'RoleRight')
    admin_role = Role.objects.filter(name='IMIS Administrator').first()
    if not admin_role:
        return
    for right in RIGHTS:
        RoleRight.objects.get_or_create(
            role=admin_role,
            right_id=right,
            defaults={'validity_from': '2020-01-01', 'audit_user_id': -1},
        )


def remove_rights_from_admin(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    RoleRight.objects.filter(right_id__in=RIGHTS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(add_rights_to_admin, remove_rights_from_admin),
    ]
