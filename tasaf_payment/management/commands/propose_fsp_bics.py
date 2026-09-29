"""Propose the banks' public SWIFT BICs as MUSE change requests (approved on the MUSE tab).

Re-runnable: FSPs already holding the values or with a pending change are skipped. Mobile
operators have no SWIFT BIC and are not proposed.
"""
from django.core.management.base import BaseCommand, CommandError

# Public SWIFT directories (theswiftcodes.com, Wise) and PBZ's own site, checked 2026-09-28.
BANK_BICS = {
    'NMB': ('NMB BANK', 'NMIBTZTZ'),
    'CRDB': ('CRDB BANK', 'CORUTZTZ'),
    'NBC': ('NBC BANK', 'NLCBTZTX'),
    'EQUITY': ('EQUITY BANK', 'EQBLTZTZ'),
    'PBZ': ("PEOPLE'S BANK OF ZANZIBAR", 'PBZATZTZ'),
    'TPB': ('TANZANIA COMMERCIAL BANK', 'TAPBTZTZ'),
    'AKIBA': ('AKIBA COMMERCIAL BANK', 'AKCOTZTZ'),
    'AZANIA': ('AZANIA BANK', 'AZANTZTZ'),
}


class Command(BaseCommand):
    help = "Propose the banks' public SWIFT BICs as MUSE FSP changes (applied only after approval)."

    def add_arguments(self, parser):
        parser.add_argument('--maker', required=True, help='Login name of the user proposing the changes')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be proposed')

    def handle(self, *args, **options):
        from core.models import User
        from tasaf_payment.models import MuseChangeKind, MuseChangeRequest, MuseChangeStatus
        from tasaf_payment.muse_setup import SetupError, profile_values, propose_profile

        maker = User.objects.filter(i_user__login_name=options['maker']).first()
        if not maker:
            raise CommandError(f"No user with login name {options['maker']!r}")

        for code, (bank_name, bic) in BANK_BICS.items():
            current = profile_values(code)
            if current.get('bic') == bic and current.get('bank_name') and current.get('fsp_type') == 'BANK':
                self.stdout.write(f'{code:8} already set ({bic})')
                continue
            if MuseChangeRequest.objects.filter(is_deleted=False, kind=MuseChangeKind.FSP_PROFILE,
                                                fsp_code=code, status=MuseChangeStatus.PENDING).exists():
                self.stdout.write(f'{code:8} a change is already awaiting approval — skipped')
                continue
            if options['dry_run']:
                self.stdout.write(f'{code:8} would propose {bank_name} / BANK / {bic}')
                continue
            try:
                change = propose_profile(maker, code, bank_name, 'BANK', bic)
                self.stdout.write(self.style.SUCCESS(f'{code:8} proposed {bank_name} / {bic} (change {change.id})'))
            except SetupError as exc:
                self.stdout.write(self.style.WARNING(f'{code:8} not proposed: {exc}'))
        self.stdout.write('Approve the proposals on Tasaf Payments ▸ MUSE ▸ Changes (not by the maker).')
