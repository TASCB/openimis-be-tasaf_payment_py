"""Grant the account-side action rights to the maker and the checker.

Until now 270101, 270102 and 270201 were held by IMIS Administrator alone, so every action on the
Verification and Pre-audit tabs was administrator-only while nine roles could already read them.

The grants follow existing rights rather than naming roles, which keeps the separation of duties
true by construction instead of by convention:

* **270101** run verification, **270201** run pre-audit and **270202** view the Pre-audit tab go to
  holders of **270302** (generate paylist) — the maker. Dispatching accounts to MUSE and running the
  eligibility gate are the same operational job as building the batch. 270202 travels with 270201
  because it gates the tab: the run right on its own leaves the buttons on a page the user cannot
  reach.
* **270102** approve or reject a MANUAL account goes to holders of **270303** (approve paylist) —
  the checker. Clearing a borderline name match is a review decision, not an operational one.

Because 270302 and 270303 are themselves held by different roles, no role gains both the dispatch
right and the clear right unless it already held both paylist rights — which only the
administrator does.

Reverse drops the three rights from roles that gained them here, identified by the reference
rights, while sparing anyone who held them beforehand.
"""
from django.db import migrations

MAKER_RIGHTS = (270101, 270201, 270202)
MAKER_REFERENCE = 270302

CHECKER_RIGHTS = (270102,)
CHECKER_REFERENCE = 270303

# Identifies who held these rights before this migration. 270004 is delete-payment-account:
# administrator-only, and never granted by any of the rights migrations, so it stays a reliable
# marker. 270001 would NOT work — migration 0012 gave it to nine roles.
PRE_EXISTING_MARKER = 270004


def _grant(cursor, right, reference):
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


def grant(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for right in MAKER_RIGHTS:
            _grant(cursor, right, MAKER_REFERENCE)
        for right in CHECKER_RIGHTS:
            _grant(cursor, right, CHECKER_REFERENCE)


def revoke(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for rights, reference in ((MAKER_RIGHTS, MAKER_REFERENCE),
                                  (CHECKER_RIGHTS, CHECKER_REFERENCE)):
            for right in rights:
                cursor.execute(
                    """
                    DELETE FROM "tblRoleRight"
                    WHERE "RightID" = %s
                      AND "RoleID" IN (
                          SELECT rr."RoleID" FROM "tblRoleRight" rr
                          WHERE rr."RightID" = %s AND rr."ValidityTo" IS NULL
                      )
                      AND "RoleID" NOT IN (
                          SELECT rr."RoleID" FROM "tblRoleRight" rr
                          WHERE rr."RightID" = %s AND rr."ValidityTo" IS NULL
                      )
                    """,
                    [right, reference, PRE_EXISTING_MARKER],
                )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0012_payment_account_search_read_set'),
    ]

    operations = [migrations.RunPython(grant, revoke)]
