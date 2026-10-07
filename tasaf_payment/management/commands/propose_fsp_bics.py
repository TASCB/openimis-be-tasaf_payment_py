"""Propose MUSE's FSP bank names and BICs as MUSE change requests (approved on the MUSE tab).

Re-runnable: FSPs already holding the values or with a pending change are skipped.
"""
from django.core.management.base import BaseCommand, CommandError

# From MUSE's FSP BIC list (BIC--.xlsx, received 2026-10-01).
MUSE_FSPS = {
    'AKIBA': ('AKIBA COMMERCIAL BANK LTD', 'BANK', 'AKCOTZTZ'),
    'AZANIA': ('AZANIA BANK LIMITED', 'BANK', 'AZANTZTZ'),
    'CRDB': ('CRDB BANK PLC', 'BANK', 'CORUTZTZ'),
    'EQUITY': ('EQUITY BANK TANZANIA LIMITED', 'BANK', 'EQBLTZTZ'),
    'NBC': ('NATIONAL BANK OF COMMERCE LTD', 'BANK', 'NLCBTZTZ'),
    'NMB': ('NATIONAL MICROFINANCE BANK LIMITED', 'BANK', 'NMIBTZTZ'),
    'PBZ': ("PEOPLE'S BANK OF ZANZIBAR LTD", 'BANK', 'PBZATZTZ'),
    'TPB': ('TANZANIA POSTAL BANK', 'BANK', 'TAPBTZTZ'),
    'AIRTELMONEY': ('AIRTEL', 'MOBILE', 'AMTLTZTX'),
    'HALOPESA': ('HALOTEL', 'MOBILE', 'HALOTZTX'),
    'MPESA': ('VODACOM', 'MOBILE', 'VODATZTX'),
    'TIGOPESA': ('YAS', 'MOBILE', 'TIGOTZTX'),
}


class Command(BaseCommand):
    help = "Propose MUSE's FSP bank names and BICs as MUSE FSP changes (applied only after approval)."

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

        for code, (bank_name, fsp_type, bic) in MUSE_FSPS.items():
            current = profile_values(code)
            if (current.get('bic'), current.get('bank_name'), current.get('fsp_type')) == (bic, bank_name, fsp_type):
                self.stdout.write(f'{code:12} already set ({bic})')
                continue
            if MuseChangeRequest.objects.filter(is_deleted=False, kind=MuseChangeKind.FSP_PROFILE,
                                                fsp_code=code, status=MuseChangeStatus.PENDING).exists():
                self.stdout.write(f'{code:12} a change is already awaiting approval — skipped')
                continue
            if options['dry_run']:
                self.stdout.write(f'{code:12} would propose {bank_name} / {fsp_type} / {bic}')
                continue
            try:
                change = propose_profile(maker, code, bank_name, fsp_type, bic,
                                         reason="From MUSE's FSP BIC list received on 2026-10-01")
                self.stdout.write(self.style.SUCCESS(f'{code:12} proposed {bank_name} / {bic} (change {change.id})'))
            except SetupError as exc:
                self.stdout.write(self.style.WARNING(f'{code:12} not proposed: {exc}'))
        self.stdout.write('Approve the proposals on Tasaf Payments ▸ MUSE ▸ Changes (not by the maker).')
