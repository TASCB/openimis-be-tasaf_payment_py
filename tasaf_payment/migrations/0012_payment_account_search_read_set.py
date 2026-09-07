"""Grant payment-account search (270001) to the paylist-search read set (270301).

270001 gated the whole Verification tab, and it was held by IMIS Administrator alone. That
left the account half of the pipeline invisible to every operational role — a Disbursement
Maker could generate a paylist but could not see or verify the accounts feeding it.

270301 is the existing answer to "who may read the payment pipeline": Council Coordinator,
Disbursement Maker, Finance Manager (Reviewer), Grievance Officer, Internal Auditor,
M&E Manager, M&E Officer, Payment Approver and the administrator. Payment accounts are the
same pipeline one stage earlier, so the same set reads them.

This is a READ right only, and deliberately so. The actions on the Verification tab stay
where they are:

    Send to MUSE / Verify whole area   270101
    Confirm match / Reject             270102
    Run pre-audit                      270201
    Create / update / delete account   270002 / 270003 / 270004

None of those are touched here, so a role gaining 270001 sees the tab and its rows and gets
no action buttons. Deciding who may dispatch versus who may clear a verification is a
segregation-of-duties question and is left to a separate, deliberate grant.
"""
from django.db import migrations

ACCOUNT_SEARCH_RIGHT = 270001
REFERENCE_RIGHT = 270301


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
            [ACCOUNT_SEARCH_RIGHT, REFERENCE_RIGHT, ACCOUNT_SEARCH_RIGHT],
        )


def revoke(apps, schema_editor):
    """Revoke only what this migration added.

    IMIS Administrator held 270001 before it ran and must keep it, so the reverse drops the
    right from every role in the reference set except those that already had it. The
    administrator is identified by holding an account right this migration never grants.
    """
    with schema_editor.connection.cursor() as cursor:
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
                  WHERE rr."RightID" = 270002 AND rr."ValidityTo" IS NULL
              )
            """,
            [ACCOUNT_SEARCH_RIGHT, REFERENCE_RIGHT],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0011_pre_audit_view_right'),
    ]

    operations = [migrations.RunPython(grant, revoke)]
