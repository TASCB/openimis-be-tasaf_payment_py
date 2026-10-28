"""Bulk account check (270104) -> PCT Officer, PCT Manager, IMIS Administrator."""
from django.db import migrations

CHECK = 270104
ROLES = ('PCT Officer', 'PCT Manager', 'IMIS Administrator')


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for role in ROLES:
            cursor.execute(
                """
                INSERT INTO "tblRoleRight" ("RoleID", "RightID", "ValidityFrom", "AuditUserId")
                SELECT r."RoleID", %s, NOW(), 1
                FROM "tblRole" r
                WHERE r."RoleName" = %s AND r."ValidityTo" IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM "tblRoleRight" x
                      WHERE x."RoleID" = r."RoleID" AND x."RightID" = %s AND x."ValidityTo" IS NULL
                  )
                """,
                [CHECK, role, CHECK],
            )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('DELETE FROM "tblRoleRight" WHERE "RightID" = %s', [CHECK])


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0042_account_check'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
