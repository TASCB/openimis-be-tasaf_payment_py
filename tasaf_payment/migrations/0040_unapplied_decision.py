"""The PCT decides each unapplied payment (Repay / Send to Case / Withdraw).

* 270402 decide unapplied payments -> PCT Officer, PCT Manager, IMIS Administrator; the PCT roles
  also get what the page needs to read: 270001 accounts, 270301 paylists, 270401 returned payments.
* Unapplied payees the earlier code already sent to Case Management are recorded as CASE, so they
  keep their correction instead of waiting for a decision.
"""
from django.db import migrations

DECIDE = 270402
PCT_ROLES = ('PCT Officer', 'PCT Manager')
GRANTS = {role: (DECIDE, 270001, 270301, 270401) for role in PCT_ROLES}
GRANTS['IMIS Administrator'] = (DECIDE,)


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for role, rights in GRANTS.items():
            for right in rights:
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
                    [right, role, right],
                )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('DELETE FROM "tblRoleRight" WHERE "RightID" = %s', [DECIDE])


def mark_routed_as_case(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE "tasaf_PaylistItem"
            SET "Json_ext" = "Json_ext" || jsonb_build_object('unapplied_action',
                jsonb_build_object('action', 'CASE', 'by', 'migration 0040', 'at', NOW()::text))
            WHERE status = 'UNAPPLIED' AND "Json_ext" ? 'returned_at' AND "Json_ext" ? 'follow_up_id'
              AND NOT "Json_ext" ? 'unapplied_action'
            """
        )


def unmark(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE "tasaf_PaylistItem" SET "Json_ext" = "Json_ext" - 'unapplied_action'
            WHERE "Json_ext" -> 'unapplied_action' ->> 'by' = 'migration 0040'
            """
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0039_gepg_message_log'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
        migrations.RunPython(mark_routed_as_case, unmark),
    ]
