from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Announce unapplied withdrawals MUSE has not answered after withdrawal_answer_warning_minutes (30); "
            "each payee once. Run every 15 minutes. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--minutes', type=int, default=None)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from tasaf_payment.apps import TasafPaymentConfig
        from tasaf_payment.muse.withdrawal import remind

        minutes = options['minutes'] if options['minutes'] is not None else int(
            getattr(TasafPaymentConfig, 'withdrawal_answer_warning_minutes', 30) or 30)
        rows = remind(minutes, apply=options['apply'])
        for row in rows:
            self.stdout.write(f"{row['reference']}  {row['msg_id']}  sent {row['sent_at']}  unanswered")
        self.stdout.write(f'{len(rows)} withdrawal(s) unanswered after {minutes} min')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing was announced. Add --apply to do it.'))
