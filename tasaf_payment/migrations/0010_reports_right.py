"""Grant the auditor reports right (270801).

The Reports tab is gated on this right, so without the grant the tab is simply
invisible — which is what happened when it was first added.

Granted to exactly the roles that already hold the withdrawal-charge view right
(270701): the finance/audit/M&E set plus the administrator. That set is the existing
answer to "who may look at payment configuration and figures", and an auditor report
belongs with it rather than with the operational rights.
"""
from django.db import migrations

REPORTS_RIGHT = 270801
REFERENCE_RIGHT = 270701


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO "tblRoleRight" ("RoleID", "RightID", "ValidityFrom", "AuditUserId")
            SELECT DISTINCT rr."RoleID", %s, NOW(), 1
            FROM "tblRoleRight" rr
            WHERE rr."RightID" = %s
              AND rr."ValidityTo" IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM "tblRoleRight" x
                  WHERE x."RoleID" = rr."RoleID" AND x."RightID" = %s
                    AND x."ValidityTo" IS NULL
              )
            """,
            [REPORTS_RIGHT, REFERENCE_RIGHT, REPORTS_RIGHT],
        )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'DELETE FROM "tblRoleRight" WHERE "RightID" = %s', [REPORTS_RIGHT],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0009_historicalpaylist_closed_at_and_more'),
    ]

    operations = [migrations.RunPython(grant, revoke)]
