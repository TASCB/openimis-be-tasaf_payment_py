from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Release accounts whose GePG verification request got no answer in time, so they can be "
            "sent again. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=None,
                            help='Answer deadline; default gepg_answer_timeout_hours (48).')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from tasaf_payment.apps import TasafPaymentConfig
        from tasaf_payment.gepg.sweeper import expire

        hours = options['hours'] or int(TasafPaymentConfig.gepg_answer_timeout_hours or 48)
        rows = expire(hours, apply=options['apply'])
        for row in rows:
            self.stdout.write(f"{row['acc_verif_id']}  {row['fsp_code']:<12} sent {row['sent_at']}  "
                              f"{row['accounts']} account(s)")
        self.stdout.write(f'{len(rows)} request(s) older than {hours} h without an answer')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))
