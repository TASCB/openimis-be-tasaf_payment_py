from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Announce GePG verification requests that are still unanswered, and release the accounts of "
            "GePG requests and TIPS lookups past the deadline so they can be sent again. Run every 15 minutes. "
            "Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=None,
                            help='Answer deadline; default gepg_answer_timeout_hours (6).')
        parser.add_argument('--warn-minutes', type=int, default=None,
                            help='Announce a request unanswered after this long; default '
                                 'gepg_answer_warning_minutes (30).')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from tasaf_payment.apps import TasafPaymentConfig
        from tasaf_payment.gepg.sweeper import expire, remind

        def setting(option, name, default):
            return options[option] if options[option] is not None else int(getattr(TasafPaymentConfig, name, default))

        minutes = setting('warn_minutes', 'gepg_answer_warning_minutes', 30)
        hours = setting('hours', 'gepg_answer_timeout_hours', 6)
        late = remind(minutes, apply=options['apply'])
        for row in late:
            self.stdout.write(f"{row['acc_verif_id']}  {row['fsp_code']:<12} sent {row['sent_at']}  unanswered")
        self.stdout.write(f'{len(late)} request(s) newly unanswered after {minutes} min')
        rows = expire(hours, apply=options['apply'])
        for row in rows:
            self.stdout.write(f"{row['acc_verif_id']}  {row['fsp_code']:<12} sent {row['sent_at']}  "
                              f"{row['accounts']} account(s)")
        self.stdout.write(f'{len(rows)} request(s) older than {hours} h without an answer')
        from tasaf_payment.tips.sweeper import expire as expire_tips
        tips_hours = int(getattr(TasafPaymentConfig, 'tips_answer_timeout_hours', 6))
        tips = expire_tips(tips_hours, apply=options['apply'])
        for row in tips:
            self.stdout.write(f"{row['msg_id']}  {row['fsp_code']:<12} sent {row['sent_at']}  "
                              f"{row['accounts']} account(s)")
        self.stdout.write(f'{len(tips)} TIPS lookup(s) older than {tips_hours} h without an answer')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))
