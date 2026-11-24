"""Keep 270901 only on roles holding 270902; approvers decide in the approval inbox."""
from django.db import migrations

VIEW, PROPOSE = 270901, 270902
GRANTED_FROM = (270701, 270302, 270303)


def narrow(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE "tblRoleRight" rr SET "ValidityTo" = NOW()
            WHERE rr."RightID" = %s AND rr."ValidityTo" IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM "tblRoleRight" x
                  WHERE x."RoleID" = rr."RoleID" AND x."RightID" = %s AND x."ValidityTo" IS NULL
              )
            """,
            [VIEW, PROPOSE],
        )


def widen(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO "tblRoleRight" ("RoleID", "RightID", "ValidityFrom", "AuditUserId")
            SELECT DISTINCT rr."RoleID", %s, NOW(), 1
            FROM "tblRoleRight" rr
            WHERE rr."RightID" IN %s AND rr."ValidityTo" IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM "tblRoleRight" x
                  WHERE x."RoleID" = rr."RoleID" AND x."RightID" = %s AND x."ValidityTo" IS NULL
              )
            """,
            [VIEW, GRANTED_FROM, VIEW],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0048_tips_drop_service_codes'),
    ]

    operations = [migrations.RunPython(narrow, widen)]
