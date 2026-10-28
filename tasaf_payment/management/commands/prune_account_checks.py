from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Delete bulk account checks older than account_check_retention_days (90), with their rows and "
            "GePG requests. Checks still waiting for GePG are kept. Run daily. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None, help='Keep this many days; default 90.')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from tasaf_payment.account_check import prune, retention_days

        days = options['days'] or retention_days()
        rows = prune(days, apply=options['apply'])
        for row in rows:
            self.stdout.write(f"{row['uuid']}  {row['created_at']:%Y-%m-%d}  {row['created_by']:<20} "
                              f"{row['row_count']} row(s)  {row['file_name']}")
        self.stdout.write(f'{len(rows)} check(s) older than {days} days')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))
