from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = ("Withdraw the payees MUSE named when it rejected a batch, for paylists already REJECTED. "
            "Dry run unless --apply: the dry run runs the real handling and rolls it back.")

    def add_arguments(self, parser):
        parser.add_argument('--paylist', action='append', default=[],
                            help='MUSE msgId (TM...) or paylist UUID; repeat for several. Default: every '
                                 'REJECTED MUSE paylist not yet processed.')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        from django.db.models import Q
        from tasaf_payment.models import Paylist, PaylistStatus, PaymentDestination
        from tasaf_payment.muse.rejection import handle_batch_rejection

        qs = Paylist.objects.filter(is_deleted=False, destination=PaymentDestination.MUSE,
                                    status=PaylistStatus.REJECTED).exclude(muse_msg_id__isnull=True)
        if options['paylist']:
            wanted = Q()
            for ref in options['paylist']:
                wanted |= Q(muse_msg_id=ref) | Q(uuid=ref) if _is_uuid(ref) else Q(muse_msg_id=ref)
            qs = qs.filter(wanted)

        apply = options['apply']
        for paylist in qs.order_by('submitted_at'):
            done = {r.get('msg_id') for r in (paylist.json_ext or {}).get('muse_rejections') or []}
            if paylist.muse_msg_id in done:
                self.stdout.write(f'{paylist.muse_msg_id}: already processed, skipped')
                continue
            with transaction.atomic():
                record = handle_batch_rejection(paylist, paylist.muse_status_desc)
                if not apply:
                    transaction.set_rollback(True)
            self._report(paylist, record)

        if not apply:
            self.stdout.write(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))

    def _report(self, paylist, record):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{paylist.muse_msg_id} ({paylist.muse_batch_reference}): {record['withdrawn']} withdrawn, "
            f"{record['kept']} kept, {record['needs_review']} need review"))
        for row in record['payees']:
            self.stdout.write(f"  {row['action']:<12} {row['account']:<16} {row['class']:<9} "
                              f"{row['reason']}{'  (' + row['note'] + ')' if row.get('note') else ''}")
        if record['unparsed']:
            self.stdout.write(f"  NEEDS_REVIEW not understood: {record['unparsed']}")


def _is_uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False
