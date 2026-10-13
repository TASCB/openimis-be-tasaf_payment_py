from django.db import migrations

RIGHTS = (202001, 200001)
ROLE_SUFFIX = 'Grievance Officer'


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for right in RIGHTS:
            cursor.execute(
                """
                INSERT INTO "tblRoleRight" ("RoleID", "RightID", "ValidityFrom", "AuditUserId")
                SELECT DISTINCT r."RoleID", %s, NOW(), 1
                FROM "tblRole" r
                WHERE r."ValidityTo" IS NULL AND r."RoleName" ILIKE %s
                  AND NOT EXISTS (
                      SELECT 1 FROM "tblRoleRight" x
                      WHERE x."RoleID" = r."RoleID" AND x."RightID" = %s AND x."ValidityTo" IS NULL
                  )
                """,
                [right, f'%{ROLE_SUFFIX}', right],
            )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for right in RIGHTS:
            cursor.execute(
                """
                DELETE FROM "tblRoleRight"
                WHERE "RightID" = %s AND "RoleID" IN (
                    SELECT r."RoleID" FROM "tblRole" r
                    WHERE r."ValidityTo" IS NULL AND r."RoleName" ILIKE %s)
                """,
                [right, f'%{ROLE_SUFFIX}'],
            )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0029_dtb_provider'),
    ]

    operations = [migrations.RunPython(grant, revoke)]
