"""Move the tasaf_payment rights off 152xxx (152101-152103 collide with contract) onto 270xxx."""
from django.db import migrations
from django.utils import timezone

# tasaf_payment is the only claimant -> close old, open new
EXCLUSIVE = {
    152001: 270001,
    152002: 270002,
    152003: 270003,
    152004: 270004,
    152201: 270201,
    152301: 270301,
    152302: 270302,
    152303: 270303,
    152304: 270304,
    152401: 270401,
    152501: 270501,
    152601: 270601,
}

# shared with contract -> add the new code, leave the old row open
SHARED = {
    152101: 270101,
    152102: 270102,
    152103: 270103,
}


def _open(RoleRight, role_id, right_id):
    return RoleRight.objects.filter(
        role_id=role_id, right_id=right_id, validity_to__isnull=True)


def _grant(RoleRight, role_id, right_id):
    if not _open(RoleRight, role_id, right_id).exists():
        RoleRight.objects.create(role_id=role_id, right_id=right_id, audit_user_id=-1)


def renumber(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    now = timezone.now()

    for old, new in EXCLUSIVE.items():
        rows = RoleRight.objects.filter(right_id=old, validity_to__isnull=True)
        for role_id in list(rows.values_list('role_id', flat=True)):
            _grant(RoleRight, role_id, new)
        rows.update(validity_to=now)

    payment_roles = set(
        RoleRight.objects
        .filter(right_id__in=list(EXCLUSIVE.values()), validity_to__isnull=True)
        .values_list('role_id', flat=True))

    for old, new in SHARED.items():
        for role_id in payment_roles:
            if _open(RoleRight, role_id, old).exists():
                _grant(RoleRight, role_id, new)

    _clear_cache()


def restore(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')

    RoleRight.objects.filter(
        right_id__in=list(EXCLUSIVE.values()) + list(SHARED.values()),
        validity_to__isnull=True,
    ).delete()

    for old in EXCLUSIVE:
        RoleRight.objects.filter(right_id=old, validity_to__isnull=False).update(validity_to=None)

    _clear_cache()


def _clear_cache():
    try:
        from django.core.cache import cache
        if hasattr(cache, 'delete_pattern'):
            cache.delete_pattern('rights_*')
        else:
            cache.clear()
    except Exception:  # pragma: no cover - cache backend may be unavailable
        pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasaf_payment', '0003_historicalpaylist_batch_group_and_more'),
    ]

    operations = [
        migrations.RunPython(renumber, restore),
    ]
