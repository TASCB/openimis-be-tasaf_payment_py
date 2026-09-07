"""Split pre-audit viewing (270202) from running it (270201).

Pre-audit was the only workspace tab gated on a *run* right, so an auditor either saw
nothing or was given 270201 and could then operate the eligibility gate they are meant to
be checking. Every other tab is gated on a search right; this makes pre-audit match, and
follows the precedent already set by the reports right (270801), which apps.py records as
"deliberately separate from the operational rights".

Granted to the union of two existing sets, so nobody loses access and auditors gain it:

* holders of 270201 — the operators who can already see and run pre-audit today. Without
  this they would lose the tab the moment it is regated;
* holders of 270801 — the reports/audit set, which is the existing answer to "who may
  read payment figures without operating the pipeline".

Note this grants the *view* right only. Running pre-audit still requires 270201, which is
untouched.
"""
from django.db import migrations

PRE_AUDIT_VIEW_RIGHT = 270202
REFERENCE_RIGHTS = (270201, 270801)


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO "tblRoleRight" ("RoleID", "RightID", "ValidityFrom", "AuditUserId")
            SELECT DISTINCT rr."RoleID", %s, NOW(), 1
            FROM "tblRoleRight" rr
            WHERE rr."RightID" IN %s
              AND rr."ValidityTo" IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM "tblRoleRight" x
                  WHERE x."RoleID" = rr."RoleID" AND x."RightID" = %s
                    AND x."ValidityTo" IS NULL
              )
            """,
            [PRE_AUDIT_VIEW_RIGHT, REFERENCE_RIGHTS, PRE_AUDIT_VIEW_RIGHT],
        )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'DELETE FROM "tblRoleRight" WHERE "RightID" = %s', [PRE_AUDIT_VIEW_RIGHT],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0010_reports_right'),
    ]

    operations = [migrations.RunPython(grant, revoke)]
