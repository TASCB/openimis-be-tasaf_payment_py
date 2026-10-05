"""Give every payment account its FSP's main name; old spellings are kept as other names (FspMapping)."""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count


class Command(BaseCommand):
    help = "Rewrite PaymentAccount.fsp_name to each FSP's main name. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from core.models import User
        from tasaf_payment.charges import ChargeError, normalise_fsp, resolve_fsp_code
        from tasaf_payment.models import FspMapping, PaymentAccount

        user = User.objects.filter(username='Admin').first()
        usage = defaultdict(dict)
        for name, n in (PaymentAccount.objects.filter(is_deleted=False).exclude(fsp_name__isnull=True)
                        .exclude(fsp_name='').values_list('fsp_name').annotate(n=Count('id'))
                        .values_list('fsp_name', 'n')):
            try:
                usage[resolve_fsp_code(name)][name] = n
            except ChargeError:
                continue

        mapped = set(FspMapping.objects.filter(is_deleted=False).values_list('fsp_name_key', flat=True))
        plan = []
        for code, names in sorted(usage.items()):
            main = max(names, key=names.get)
            for name, n in sorted(names.items()):
                if name != main:
                    plan.append((code, name, main, n, normalise_fsp(name) not in mapped))

        if not plan:
            self.stdout.write(self.style.SUCCESS('Every FSP already uses one name.'))
            return
        for code, name, main, n, new_mapping in plan:
            extra = ' (+ other name mapping)' if new_mapping else ''
            self.stdout.write(f'  {code:<12} {name!r:<26} -> {main!r:<26} {n:>6} account(s){extra}')
        total = sum(p[3] for p in plan)
        if not options['apply']:
            self.stdout.write(f'Dry run: {total} account(s) would change. Re-run with --apply.')
            return

        with transaction.atomic():
            for code, name, main, _n, new_mapping in plan:
                if new_mapping:
                    FspMapping(fsp_name=name, fsp_code=code).save(username=user.username if user else None)
                PaymentAccount.objects.filter(is_deleted=False, fsp_name=name).update(fsp_name=main)
        self.stdout.write(self.style.SUCCESS(f'Applied: {total} account(s) renamed.'))
