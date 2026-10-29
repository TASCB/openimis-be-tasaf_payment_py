"""Manage unapplied payments (270403: repay despite an account problem, overdue reminders)
-> PCT Manager, IMIS Administrator."""
from django.db import migrations

RIGHT = 270403
ROLES = ('PCT Manager', 'IMIS Administrator')


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
                [RIGHT, role, RIGHT],
            )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('DELETE FROM "tblRoleRight" WHERE "RightID" = %s', [RIGHT])


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0044_account_check_dormant'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
