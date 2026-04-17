from django.db import migrations

# Rights added in 0002: 152001-152004, 152101-152105
# Rights missing from 0002 and added here:
MISSING_RIGHTS = [
    152201,  # run pre-audit
    152301,  # search paylists
    152302,  # generate paylist
    152303,  # approve paylist
    152304,  # submit paylist
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
    for right in MISSING_RIGHTS:
        RoleRight.objects.get_or_create(
            role=admin_role,
            right_id=right,
            defaults={'validity_from': '2020-01-01', 'audit_user_id': -1},
        )


def remove_rights_from_admin(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    RoleRight.objects.filter(right_id__in=MISSING_RIGHTS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0004_muse_flow_models'),
    ]

    operations = [
        migrations.RunPython(add_rights_to_admin, remove_rights_from_admin),
    ]
