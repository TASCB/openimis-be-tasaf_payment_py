"""Service-signal hooks for TASAF payment business rules."""

import logging

from django.core.exceptions import ValidationError

from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

logger = logging.getLogger(__name__)


def bind_service_signals():
    """Wire tasaf_payment signal handlers."""

    def apply_fsp_charges_on_approval(**kwargs):
        """Approving an FSP tariff task replaces that FSP's whole band set atomically."""
        from tasaf_payment.charges import FSP_CHARGES_EVENT, apply_band_set
        try:
            task = kwargs.get('result', {}).get('data', {}).get('task')
            if not task or task.get('business_event') != FSP_CHARGES_EVENT:
                return
            if (task.get('status') or '').upper() != 'COMPLETED':
                return
            payload = task.get('data') or {}
            bands = payload.get('bands') or []
            if not bands:
                return
            from core.models import User
            user = User.objects.filter(id=task.get('user_updated_id')).first()
            apply_band_set(payload['fsp_code'], bands, user, payload.get('effective_from'))
            logger.info("tasaf_payment: applied %d band(s) for %s after approval",
                        len(bands), payload['fsp_code'])
        except Exception:
            logger.error("tasaf_payment: failed to apply approved FSP charges", exc_info=True)

    bind_service_signal(
        'task_service.complete_task',
        apply_fsp_charges_on_approval,
        bind_type=ServiceSignalBindType.AFTER,
    )

    # Approving a tariff-change task runs the real write, mirroring PmtGlobalFormulaService.
    try:
        from tasks_management.services import on_task_complete_service_handler
        from tasaf_payment.services import WithdrawalChargeService
        bind_service_signal(
            'task_service.complete_task',
            on_task_complete_service_handler(WithdrawalChargeService),
            bind_type=ServiceSignalBindType.AFTER,
        )
    except Exception:
        logger.warning("tasaf_payment: charge maker-checker binding skipped", exc_info=True)

    def check_all_accounts_verified(**kwargs):
        """Block payroll creation when active households lack verified primary accounts."""
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

            verified_account_sq = PaymentAccount.objects.filter(
                group_beneficiary=OuterRef('pk'),
                verification_status=VerificationStatus.VERIFIED,
                is_primary=True,
                is_deleted=False,
            )

            unverified_qs = GroupBeneficiary.objects.filter(
                benefit_plan_id=benefit_plan_id,
                status=BeneficiaryStatus.ACTIVE,
                is_deleted=False,
            ).exclude(Exists(verified_account_sq))

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

    try:
        from tasaf_payment.approval_adapter import PaylistApprovalAdapter
        bind_service_signal(
            'approval_service.finalized',
            PaylistApprovalAdapter.on_finalized,
            bind_type=ServiceSignalBindType.AFTER,
        )
    except Exception as exc:
        logger.warning("tasaf_payment: approval-engine binding skipped (%s)", exc)
