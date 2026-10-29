from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Announce to the PCT Managers the unapplied payments still undecided after "
            "unapplied_reminder_days (7); each payment once. Run daily. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from tasaf_payment.apps import TasafPaymentConfig
        from tasaf_payment.returned import remind_overdue

        days = options['days'] if options['days'] is not None else int(
            getattr(TasafPaymentConfig, 'unapplied_reminder_days', 7) or 7)
        rows = remind_overdue(days, apply=options['apply'])
        for row in rows:
            self.stdout.write(f"{row['paylist']}  {row['count']} payment(s) undecided")
        self.stdout.write(f"{sum(r['count'] for r in rows)} payment(s) undecided for more than {days} day(s)")
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing was announced. Add --apply to do it.'))
