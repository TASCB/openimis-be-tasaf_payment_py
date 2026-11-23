"""Propose the TIPS settings and TIPS code per provider; applied only after approval. Never changes the route."""
from django.core.management.base import BaseCommand, CommandError

# From MoF's TIPS identifier types and FSP codes list (received 2026-11-20).
TIPS_FSP_CODES = {
    'AKIBA': '012', 'AZANIA': '031', 'CRDB': '003', 'DTB': '011', 'EQUITY': '047', 'NBC': '015',
    'NMB': '016', 'PBZ': '004', 'TPB': '048', 'TIGOPESA': '501', 'EZYPESA': '502', 'MPESA': '503',
    'AIRTELMONEY': '504', 'TTCL': '505', 'HALOPESA': '506',
}
MOF_SENDER, TIPS_RECEIVER = 'MOFPTZT0', 'TANZTZT0'
MOF_TEST_SUMMARY = ('BANK', '0011055818', '013')


class Command(BaseCommand):
    help = 'Propose the TIPS settings and TIPS code per provider (applied only after approval).'

    def add_arguments(self, parser):
        parser.add_argument('--maker', required=True, help='Login name of the user proposing the change')
        parser.add_argument('--summary', help='Bulk summary account as TYPE:NUMBER:FSP, e.g. BANK:0011055818:013')
        parser.add_argument('--sender', help=f'Sender code (default: current, else {MOF_SENDER})')
        parser.add_argument('--receiver', help=f'Receiver code (default: current, else {TIPS_RECEIVER})')
        parser.add_argument('--reason', default="From MoF's TIPS FSP code list received on 2026-11-20")
        parser.add_argument('--dry-run', action='store_true', help='Show what would be proposed')

    def handle(self, *args, **options):
        from core.models import User
        from tasaf_payment.muse.setup import SetupError, provider_list
        from tasaf_payment.tips import setup as tips_setup

        maker = User.objects.filter(i_user__login_name=options['maker']).first()
        if not maker:
            raise CommandError(f"No user with login name {options['maker']!r}")

        current = tips_setup.settings_values()
        data = {**current}
        data['sender'] = options['sender'] or current.get('sender') or MOF_SENDER
        data['receiver'] = options['receiver'] or current.get('receiver') or TIPS_RECEIVER
        if options['summary']:
            parts = options['summary'].split(':')
            if len(parts) != 3:
                raise CommandError('--summary must be TYPE:NUMBER:FSP')
            summary = parts
        elif current.get('summary_identifier'):
            summary = (current.get('summary_identifier_type'), current['summary_identifier'],
                       current.get('summary_fsp_code'))
        else:
            summary = MOF_TEST_SUMMARY
            self.stdout.write(self.style.WARNING("No summary account given: using MoF's test account "
                                                 f"{':'.join(MOF_TEST_SUMMARY)}. Pass --summary for production."))
        data['summary_identifier_type'], data['summary_identifier'], data['summary_fsp_code'] = summary
        data['verification_route'] = current.get('verification_route') or 'GEPG'

        ours = {p['fsp_code']: p['accounts'] for p in provider_list()}
        codes = dict(current.get('fsp_codes') or {})
        for fsp, accounts in sorted(ours.items()):
            tips = TIPS_FSP_CODES.get(fsp)
            if not tips:
                self.stdout.write(self.style.WARNING(f'{fsp:12} no TIPS code known ({accounts} accounts): left as is'))
                continue
            note = 'unchanged' if codes.get(fsp) == tips else f"{codes.get(fsp) or '(none)'} -> {tips}"
            self.stdout.write(f'{fsp:12} {note}')
            codes[fsp] = tips
        data['fsp_codes'] = codes

        self.stdout.write(f"sender {data['sender']}, receiver {data['receiver']}, summary {':'.join(summary)}, "
                          f"route {data['verification_route']} (not changed)")
        if options['dry_run']:
            self.stdout.write('Dry run: nothing proposed.')
            return
        try:
            change = tips_setup.propose_settings(maker, data, reason=options['reason'])
        except SetupError as exc:
            raise CommandError(f'Not proposed: {exc}')
        self.stdout.write(self.style.SUCCESS(f'Proposed (change {change.id}). Approve it on Payments ▸ '
                                             'Integrations ▸ TIPS ▸ Changes (not by the maker).'))
