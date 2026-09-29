"""Grant the MUSE settings rights, each following an existing right.

* 270901 view the MUSE tab   -> holders of 270701 (view charges), 270302 and 270303
* 270902 propose a change    -> holders of 270302 (generate paylist) -- the payment maker
* 270903 approve a change    -> holders of 270303 (approve paylist)  -- the payment checker

Maker and checker follow the paylist pair, so no role gains both unless it already held both.
270702 was not used for the maker: it is also held by the auditor and M&E roles.
The rights are new, so reverse removes them outright.
"""
from django.db import migrations

GRANTS = ((270901, 270701), (270901, 270302), (270901, 270303), (270902, 270302), (270903, 270303))


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for right, reference in GRANTS:
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
                [right, reference, right],
            )


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('DELETE FROM "tblRoleRight" WHERE "RightID" IN %s',
                       [tuple({r for r, _ in GRANTS})])


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0015_muse_change_request'),
    ]

    operations = [migrations.RunPython(grant, revoke)]
