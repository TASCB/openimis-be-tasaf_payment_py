"""
tasaf_payment.signals
======================
Service-signal hooks that enforce TASAF business rules without modifying
core openIMIS modules.

Payroll creation guard
-----------------------
Before any payroll is created (`payroll_service.create` BEFORE signal), this
module checks that every active GroupBeneficiary enrolled in the target
benefit plan has at least one VERIFIED primary PaymentAccount.

If unverified households are found the signal raises ValidationError, which
causes PayrollService.create() to surface the error to the caller.

The guard only runs for GROUP-type benefit plans (the TASAF household model).
Individual-type plans have no PaymentAccount records and are skipped silently.

Connectivity note
-----------------
Django crawls module.signals paths and calls bind_service_signals() after all
apps are loaded.  Signals may be queued before payroll's PayrollService is
imported — the RegisteredServiceSignal queue mechanism handles this safely.
"""

import logging

from django.core.exceptions import ValidationError

from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

logger = logging.getLogger(__name__)


def bind_service_signals():
    """
    Wire all tasaf_payment signal handlers.  Called by openIMIS core after
    all apps are ready.
    """

    def check_all_accounts_verified(**kwargs):
        """
        BEFORE hook for `payroll_service.create`.

        Blocks payroll creation when active GroupBeneficiary records exist
        for the target benefit plan but have no verified primary PaymentAccount.

        Signal kwargs structure (from register_service_signal decorator):
            data = [(positional_args_tuple), {keyword_args_dict}]
            data[0][0] == obj_data passed to PayrollService.create()
        """
        data = kwargs.get('data', [[], {}])
        try:
            obj_data = data[0][0]
        except (IndexError, TypeError):
            return  # malformed signal — skip guard

        payment_plan_id = obj_data.get('payment_plan_id')
        if not payment_plan_id:
            logger.debug(
                "check_all_accounts_verified: no payment_plan_id in payload — skipping guard"
            )
            return

        try:
            from django.db.models import Exists, OuterRef
            from contribution_plan.models import PaymentPlan
            from social_protection.models import BeneficiaryStatus, GroupBeneficiary
            from tasaf_payment.models import PaymentAccount, VerificationStatus

            payment_plan = (
                PaymentPlan.objects
                .filter(id=payment_plan_id)
                .select_related('benefit_plan')
                .first()
            )
            if not payment_plan or not payment_plan.benefit_plan:
                logger.debug(
                    "check_all_accounts_verified: payment_plan %s or its benefit_plan not found",
                    payment_plan_id,
                )
                return

            benefit_plan    = payment_plan.benefit_plan
            benefit_plan_id = benefit_plan.id

            # Guard only applies to GROUP-type plans (TASAF household model).
            # Individual-type plans have no GroupBeneficiary → PaymentAccount chain.
            has_group_beneficiaries = GroupBeneficiary.objects.filter(
                benefit_plan_id=benefit_plan_id,
                is_deleted=False,
            ).exists()
            if not has_group_beneficiaries:
                logger.debug(
                    "check_all_accounts_verified: benefit_plan %s has no GroupBeneficiary records — skipping",
                    benefit_plan_id,
                )
                return

            # Subquery: does this GroupBeneficiary have a verified primary account?
            verified_account_sq = PaymentAccount.objects.filter(
                group_beneficiary=OuterRef('pk'),
                verification_status=VerificationStatus.VERIFIED,
                is_primary=True,
                is_deleted=False,
            )

            # All ACTIVE households missing a verified account
            unverified_qs = GroupBeneficiary.objects.filter(
                benefit_plan_id=benefit_plan_id,
                status=BeneficiaryStatus.ACTIVE,
                is_deleted=False,
            ).exclude(Exists(verified_account_sq))

            # Use .count() so the number appears in the error message.
            # The Exists-based exclude() hits the index on
            # (group_beneficiary_id, verification_status, is_primary, is_deleted)
            # so it scales to millions of rows without a full scan.
            unverified_count = unverified_qs.count()

            if unverified_count > 0:
                logger.warning(
                    "check_all_accounts_verified: blocking payroll creation — "
                    "%d household(s) in benefit_plan '%s' (%s) have no verified "
                    "primary PaymentAccount",
                    unverified_count,
                    benefit_plan.code,
                    benefit_plan_id,
                )
                raise ValidationError(
                    f"Payroll blocked: {unverified_count:,} household(s) enrolled "
                    f"in benefit plan '{benefit_plan.code}' have no verified primary "
                    f"payment account. Run NIDA verification and resolve any "
                    f"MANUAL/FAILED accounts before creating this payroll."
                )

            logger.info(
                "check_all_accounts_verified: all active households in benefit_plan '%s' "
                "have verified accounts — payroll creation allowed",
                benefit_plan.code,
            )

        except ValidationError:
            raise  # propagate — intentional block

        except Exception as exc:
            # Never silently swallow guard errors — log at ERROR so operators
            # see the problem, but do NOT block payroll creation on an
            # infrastructure failure (e.g. DB timeout during the check).
            logger.error(
                "check_all_accounts_verified: unexpected error in payroll guard — "
                "allowing payroll creation to proceed. Error: %s",
                exc,
                exc_info=True,
            )

    bind_service_signal(
        'payroll_service.create',
        check_all_accounts_verified,
        bind_type=ServiceSignalBindType.BEFORE,
    )
