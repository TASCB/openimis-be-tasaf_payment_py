from datetime import date

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Lapse withdrawn payees not paid by their payment cycle's cut-off and hand the payrolls they "
            "held to reconciliation. Dry run unless --apply. Meant to run daily (cron).")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--date', help='Treat this day (YYYY-MM-DD) as today; default today in EAT.')

    def handle(self, *args, **options):
        from tasaf_payment.withdrawn_hold import release_expired_holds

        today = date.fromisoformat(options['date']) if options.get('date') else None
        released = release_expired_holds(today=today, apply=options['apply'])
        if not released:
            self.stdout.write('No withdrawn payee is past its cut-off.')
        for payroll_id, items in released.items():
            self.stdout.write(self.style.MIGRATE_HEADING(f'Payroll {payroll_id}: {len(items)} payee(s) lapse'))
            for item in items:
                self.stdout.write(f'  {item.payment_account.account_number:<16} paylist '
                                  f'{item.paylist.muse_batch_reference or item.paylist_id}')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))
