"""Realistic PCT disbursement demo data, consistent with calcrule_pct_payment.

Prices households with the same arithmetic and defaults as the rule (cap included),
spreads them across the real EPAYMENT_CODEs, and gives them realistic terminal statuses
so success and failure both have data.

Also re-derives pre-existing demo rows whose amounts were picked independently of the
breakdown, so component and paid totals reconcile. Those belong to another seeder's tag,
so they are updated in place, never deleted.

    manage.py seed_payment_demo --households 400 [--seed N] [--undo]
"""
import random
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

SEED_TAG = 'payment_demo'

PCT = {
    'base_amount': Decimal('14000'),
    'disability_top_up': Decimal('5000'),
    'young_child_unit': Decimal('7000'),
    'young_child_cap_count': 3,
    'primary_unit': Decimal('5000'),
    'primary_cap_count': 3,
    'secondary_unit': Decimal('8000'),
    'secondary_cap_count': 3,
    'household_cap_amount': Decimal('50000'),
}

FSPS = [
    ('Vodacom M-Pesa', 'MPESA', 'MOBILE', 26),
    ('NMB Bank', 'NMB', 'BANK', 18),
    ('CRDB Bank', 'CRDB', 'BANK', 16),
    ('Airtel Money', 'AIRTELMONEY', 'MOBILE', 12),
    ('Tigo Pesa', 'TIGOPESA', 'MOBILE', 9),
    ('Halopesa', 'HALOPESA', 'MOBILE', 6),
    ('NBC Bank', 'NBC', 'BANK', 4),
    ('TPB Bank', 'TPB', 'BANK', 3),
    ('Ezy Pesa', 'EZYPESA', 'MOBILE', 2),
    ('Equity Bank', 'EQUITY', 'BANK', 2),
    ('PBZ Bank', 'PBZ', 'BANK', 1),
    ('TTCL Pesa', 'TTCL', 'MOBILE', 1),
]

# Present in the report spreadsheet but absent from tasaf_FspMapping.
MISSING_MAPPINGS = [('IDB Bank', 'IDB'), ('PBZ Bank', 'PBZ'), ('TTCL Pesa', 'TTCL')]

STATUS_WEIGHTS = [('PROCESSED', 85), ('RETURNED', 7), ('UNAPPLIED', 5), ('PENDING', 3)]

VERIFICATION_WEIGHTS = [(1, 78), (2, 14), (0, 8)]   # VERIFIED / FAILED / PENDING

RETURN_REASONS = [
    ('R01', 'Account closed'),
    ('R02', 'Account name mismatch'),
    ('R03', 'Account dormant'),
    ('U01', 'Beneficiary did not collect within the window'),
    ('U02', 'Mobile wallet not activated'),
]


def _create(model, user, **fields):
    """objects.create() does not forward the user HistoryModel.save() demands."""
    obj = model(**fields)
    obj.save(user=user)
    return obj


def _weighted(pairs, rng):
    total = sum(w for _, w in pairs)
    pick = rng.uniform(0, total)
    upto = 0
    for value, weight in pairs:
        upto += weight
        if pick <= upto:
            return value
    return pairs[-1][0]


def household_breakdown(rng):
    """One household's PCT breakdown, computed exactly as calcrule_pct_payment does."""
    # Composition distribution — roughly the PCT caseload shape.
    young_raw = rng.choices([0, 1, 2, 3, 4], weights=[38, 30, 19, 9, 4])[0]
    primary_raw = rng.choices([0, 1, 2, 3, 4], weights=[42, 27, 18, 9, 4])[0]
    secondary_raw = rng.choices([0, 1, 2, 3], weights=[72, 19, 7, 2])[0]
    has_disability = rng.random() < 0.12

    young = min(young_raw, PCT['young_child_cap_count'])
    primary = min(primary_raw, PCT['primary_cap_count'])
    secondary = min(secondary_raw, PCT['secondary_cap_count'])

    disability_amount = PCT['disability_top_up'] if has_disability else Decimal('0')
    young_amount = PCT['young_child_unit'] * young
    primary_amount = PCT['primary_unit'] * primary
    secondary_amount = PCT['secondary_unit'] * secondary

    raw_total = (PCT['base_amount'] + disability_amount + young_amount
                 + primary_amount + secondary_amount)
    capped_total = min(raw_total, PCT['household_cap_amount'])

    return {
        'base_amount': float(PCT['base_amount']),
        'has_disability': has_disability,
        'disability_amount': float(disability_amount),
        'young_child_count_raw': young_raw,
        'young_child_count': young,
        'young_child_amount': float(young_amount),
        'primary_count_raw': primary_raw,
        'primary_count': primary,
        'primary_amount': float(primary_amount),
        'secondary_count_raw': secondary_raw,
        'secondary_count': secondary,
        'secondary_amount': float(secondary_amount),
        'raw_total': float(raw_total),
        'household_cap_amount': float(PCT['household_cap_amount']),
        'capped_total': float(capped_total),
    }


class Command(BaseCommand):
    help = "Seed realistic PCT disbursement demo data consistent with calcrule_pct_payment."

    def add_arguments(self, parser):
        parser.add_argument('--households', type=int, default=400,
                            help='How many households to disburse to (default 400).')
        parser.add_argument('--seed', type=int, default=20260904,
                            help='RNG seed, so runs are reproducible.')
        parser.add_argument('--undo', action='store_true',
                            help="Remove only rows tagged _seed='payment_demo'.")

    def handle(self, *args, **options):
        from core.models import User
        from tasaf_payment.models import (
            FspMapping, Paylist, PaylistItem, PaylistItemStatus, PaylistStatus,
            PaymentAccount, PaymentDestination, ReturnFeedback, VerificationStatus,
        )

        user = User.objects.first()
        if not user:
            self.stderr.write('No user available to attribute the seed to.')
            return

        if options['undo']:
            return self._undo()

        rng = random.Random(options['seed'])
        wanted = options['households']

        with transaction.atomic():
            self._seed_missing_mappings(user, FspMapping)
            self._normalise_legacy(user, rng)
            self._normalise_account_names(user)

            from social_protection.models import GroupBeneficiary
            from payroll.models import BenefitConsumption, BenefitConsumptionStatus

            beneficiaries = list(
                GroupBeneficiary.objects.filter(is_deleted=False)
                .select_related('group')[:wanted]
            )
            if wanted and not beneficiaries:
                self.stderr.write('No GroupBeneficiary rows to build demo payments from.')
                return
            if not wanted:
                self.stdout.write('households=0: normalised existing rows only.')
                return

            now = timezone.now()
            paylist = _create(
                Paylist, user,
                batch_type='MIXED',
                destination=PaymentDestination.MUSE,
                status=PaylistStatus.SUBMITTED,
                generated_at=now - timedelta(days=21),
                approved_at=now - timedelta(days=20),
                submitted_at=now - timedelta(days=19),
                batch_group=uuid.uuid4(),
                batch_sequence=1,
                batch_total=1,
                json_ext={'_seed': SEED_TAG},
            )

            created_accounts = created_items = 0
            status_tally = {}

            for beneficiary in beneficiaries:
                fsp_name, fsp_code, fsp_type = rng.choices(
                    FSPS, weights=[f[3] for f in FSPS],
                )[0][:3]

                account = _create(
                    PaymentAccount, user,
                    group_beneficiary=beneficiary,
                    account_number=self._account_number(fsp_type, rng),
                    account_name=self._recipient_name(beneficiary) or 'DEMO RECIPIENT',
                    verification_status=_weighted(VERIFICATION_WEIGHTS, rng),
                    fsp_name=fsp_name,
                    fsp_type=fsp_type,
                    pre_audit_status='PASSED',
                    is_primary=True,
                    json_ext={'_seed': SEED_TAG},
                )
                created_accounts += 1

                breakdown = household_breakdown(rng)
                net = Decimal(str(breakdown['capped_total']))

                benefit = _create(
                    BenefitConsumption, user,
                    individual_id=self._an_individual(beneficiary),
                    code=f"DEMO-{uuid.uuid4().hex[:10].upper()}",
                    amount=net,
                    type='Cash Transfer',
                    date_due=(now - timedelta(days=25)).date(),
                    status=BenefitConsumptionStatus.ACCEPTED,
                    json_ext={
                        '_seed': SEED_TAG,
                        'pct_breakdown': breakdown,
                        'benefit_plan_code': '002',
                        'beneficiary_group_id': str(beneficiary.group_id),
                    },
                )

                charge = self._charge_for(fsp_name, net)
                status = _weighted(STATUS_WEIGHTS, rng)
                status_tally[status] = status_tally.get(status, 0) + 1

                item = _create(
                    PaylistItem, user,
                    paylist=paylist,
                    payment_account=account,
                    benefit_consumption=benefit,
                    net_amount=net,
                    charge_amount=charge,
                    amount=net + charge,
                    status=getattr(PaylistItemStatus, status),
                    settled_at=(now - timedelta(days=rng.randint(1, 18))
                                if status == 'PROCESSED' else None),
                    muse_reference=(f"MUSE{rng.randint(10**9, 10**10 - 1)}"
                                    if status != 'PENDING' else None),
                    json_ext={'_seed': SEED_TAG},
                )
                created_items += 1

                if status in ('RETURNED', 'UNAPPLIED'):
                    code, description = rng.choice(RETURN_REASONS)
                    _create(
                        ReturnFeedback, user,
                        paylist_item=item,
                        feedback_type=status,
                        reason_code=code,
                        reason_description=description,
                    )
                    item.return_reason = description
                    item.save(user=user)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {created_accounts} account(s), {created_items} paylist item(s) "
            f"in paylist {paylist.uuid}."
        ))
        for status, count in sorted(status_tally.items()):
            self.stdout.write(f"  {status:10s} {count}")
        self.stdout.write("Run with --undo to remove exactly these rows.")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _normalise_account_names(self, user):
        """Replace HHIDs sitting in account_name with the household representative.

        The questionnaire never collected account details, so imports (and an earlier
        version of this seeder) put the household code there. MUSE matches the account
        number against a NAME, so an HHID guarantees a failed verification.
        """
        from tasaf_payment.models import PaymentAccount

        fixed = 0
        for account in (PaymentAccount.objects.filter(is_deleted=False)
                        .select_related('group_beneficiary__group')):
            name = (account.account_name or '').strip()
            if name and not name.startswith('P3-') and not name.startswith('SEEDG'):
                continue
            recipient = self._recipient_name(account.group_beneficiary)
            if not recipient or recipient == name:
                continue
            account.account_name = recipient
            account.save(user=user)
            fixed += 1
        if fixed:
            self.stdout.write(f"Replaced {fixed} account_name(s) holding an HHID.")

    def _normalise_legacy(self, user, rng):
        """Make every existing demo item agree with the calc rule.

        Two problems in the original ad-hoc rows, and they are different:
          * a benefit with no ``pct_breakdown`` at all → give it a composition;
          * a benefit WITH a breakdown whose item carries an unrelated ``net_amount``
            (the original seeder picked amounts independently) → re-derive the item
            from the breakdown.

        The calc rule's ``capped_total`` is the source of truth either way; the item's
        net/charge/gross are recomputed from it so all three reconcile. Rows belonging
        to another module's seeder are UPDATED, never deleted.
        """
        from tasaf_payment.models import PaylistItem

        touched = 0
        for item in (PaylistItem.objects.filter(is_deleted=False)
                     .select_related('benefit_consumption', 'payment_account')):
            benefit = item.benefit_consumption
            if not benefit:
                continue
            ext = dict(benefit.json_ext or {})
            breakdown = ext.get('pct_breakdown')
            benefit_dirty = False

            if not breakdown:
                benefit_dirty = True
                breakdown = household_breakdown(rng)
                ext['pct_breakdown'] = breakdown
                ext.setdefault('benefit_plan_code', '002')
                gb = getattr(item.payment_account, 'group_beneficiary', None)
                if gb is not None and getattr(gb, 'group_id', None):
                    ext['beneficiary_group_id'] = str(gb.group_id)
                benefit.json_ext = ext

            net = Decimal(str(breakdown.get('capped_total') or 0))
            if net <= 0:
                continue

            charge = self._charge_for(getattr(item.payment_account, 'fsp_name', None), net)
            if item.net_amount == net and item.charge_amount == charge and benefit.amount == net:
                continue

            # HistoryModel.save() raises when nothing changed.
            if benefit_dirty or benefit.amount != net:
                benefit.amount = net
                benefit.save(user=user)
            if (item.net_amount != net or item.charge_amount != charge
                    or item.amount != net + charge):
                item.net_amount = net
                item.charge_amount = charge
                item.amount = net + charge
                item.save(user=user)
                touched += 1

        if touched:
            self.stdout.write(f"Re-derived {touched} existing item(s) from the calc rule.")

    def _seed_missing_mappings(self, user, FspMapping):
        from tasaf_payment.charges import normalise_fsp
        added = 0
        for name, code in MISSING_MAPPINGS:
            key = normalise_fsp(name)
            if FspMapping.objects.filter(fsp_name_key=key, is_deleted=False).exists():
                continue
            _create(
                FspMapping, user,
                fsp_name=name, fsp_name_key=key, fsp_code=code,
                json_ext={'_seed': SEED_TAG}, user_created=user, user_updated=user,
            )
            added += 1
        if added:
            self.stdout.write(f"Added {added} missing FSP mapping(s).")

    @staticmethod
    def _recipient_name(beneficiary):
        """The household representative's name -- what an FSP holds the account under."""
        from individual.models import GroupIndividual
        group = getattr(beneficiary, 'group', None)
        if not group:
            return None
        row = (GroupIndividual.objects
               .filter(group=group, is_deleted=False, recipient_type='PRIMARY')
               .select_related('individual').first())
        if row and row.individual:
            name = f"{row.individual.first_name or ''} {row.individual.last_name or ''}".strip()
            if name:
                return name
        return str((group.json_ext or {}).get('primary_recipient') or '').strip() or None

    @staticmethod
    def _account_number(fsp_type, rng):
        if fsp_type == 'MOBILE':
            return f"2557{rng.randint(10**8, 10**9 - 1)}"
        return str(rng.randint(10**11, 10**12 - 1))

    @staticmethod
    def _an_individual(beneficiary):
        """The household head, or any member — BenefitConsumption requires an individual."""
        from individual.models import GroupIndividual
        link = (GroupIndividual.objects
                .filter(group_id=beneficiary.group_id, is_deleted=False)
                .values_list('individual_id', flat=True).first())
        return link

    @staticmethod
    def _charge_for(fsp_name, net):
        """0 when no band covers the amount."""
        from tasaf_payment.charges import ChargeError, gross_up
        try:
            _net, charge, _gross = gross_up(fsp_name, net)
            return charge or Decimal('0')
        except ChargeError:
            return Decimal('0')

    def _undo(self):
        from tasaf_payment.models import (
            FspMapping, Paylist, PaylistItem, PaymentAccount, ReturnFeedback,
        )
        from payroll.models import BenefitConsumption

        tag = {'json_ext__contains': {'_seed': SEED_TAG}}
        items = PaylistItem.objects.filter(**tag)
        ReturnFeedback.objects.filter(paylist_item__in=items).delete()
        benefit_ids = list(items.values_list('benefit_consumption_id', flat=True))
        account_ids = list(items.values_list('payment_account_id', flat=True))
        removed_items = items.count()
        items.delete()
        Paylist.objects.filter(**tag).delete()
        PaymentAccount.objects.filter(id__in=account_ids, **tag).delete()
        BenefitConsumption.objects.filter(id__in=benefit_ids, **tag).delete()
        FspMapping.objects.filter(**tag).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Removed {removed_items} demo paylist item(s) and their related rows."
        ))
