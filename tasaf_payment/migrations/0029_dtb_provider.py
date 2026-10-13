from django.db import migrations

CODE = 'DTB'
NAMES = (('DTB', 'DTB'), ('Diamond Trust Bank', 'DIAMONDTRUSTBANK'))
SOURCE = '{"source": "0029_dtb_provider"}'


def add(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('SELECT id FROM "core_User" ORDER BY (username = %s) DESC, id LIMIT 1', ['Admin'])
        row = cursor.fetchone()
        if not row:
            return
        user_id = row[0]
        for name, key in NAMES:
            cursor.execute(
                """
                INSERT INTO "tasaf_FspMapping" ("UUID", "isDeleted", "Json_ext", "DateCreated", "DateUpdated",
                    version, fsp_name, fsp_name_key, fsp_code, "UserCreatedUUID", "UserUpdatedUUID")
                SELECT gen_random_uuid(), false, %s::jsonb, NOW(), NOW(), 1, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM "tasaf_FspMapping" WHERE fsp_name_key = %s AND NOT "isDeleted")
                """,
                [SOURCE, name, key, CODE, user_id, user_id, key],
            )
        cursor.execute(
            """
            INSERT INTO "tasaf_FspProfile" ("UUID", "isDeleted", "Json_ext", "DateCreated", "DateUpdated",
                version, fsp_code, bank_name, fsp_type, bic, "UserCreatedUUID", "UserUpdatedUUID")
            SELECT gen_random_uuid(), false, %s::jsonb, NOW(), NOW(), 1, %s, 'DIAMOND TRUST BANK', 'BANK', '', %s, %s
            WHERE NOT EXISTS (SELECT 1 FROM "tasaf_FspProfile" WHERE fsp_code = %s AND NOT "isDeleted")
            """,
            [SOURCE, CODE, user_id, user_id, CODE],
        )


def remove(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('DELETE FROM "tasaf_FspMapping" WHERE "Json_ext" @> %s::jsonb', [SOURCE])
        cursor.execute('DELETE FROM "tasaf_FspProfile" WHERE "Json_ext" @> %s::jsonb', [SOURCE])


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0028_mobile_app_fsp_aliases'),
    ]

    operations = [migrations.RunPython(add, remove)]
