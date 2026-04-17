"""
tasaf_payment.tasks
====================
Celery tasks for MUSE-driven verification and paylist operations at scale.

Task topology
-------------

Batch verification dispatch:

    run_batch_verification_task(filters)
        │
        ├── dispatch_verification_chunk(chunk_1)  ← worker A
        ├── dispatch_verification_chunk(chunk_2)  ← worker B
        └── dispatch_verification_chunk(chunk_N)  ← worker C

Each chunk marks accounts as PENDING_MUSE and publishes verification
requests to GovESB. MUSE will push results back asynchronously via
MuseVerificationInboundService.handle_result().

Pre-audit batch:

    run_batch_pre_audit_task(account_ids)
        │
        └── pre_audit_chunk(chunk)  ← worker

TODO (GovESB): When the GovESB adaptor is available, replace the stub
publish in MuseVerificationDispatchService._publish() with the real
GovESB producer. The Celery task structure itself does not change.
"""

import logging
from itertools import islice

from celery import shared_task

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 100


# ─── helpers ──────────────────────────────────────────────────────────────────

def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            break
        yield chunk


def _build_account_queryset(filters: dict):
    """
    Build a PaymentAccount queryset from flexible filter criteria.

    Supported filters:
        account_ids:         list[int]  — explicit IDs (from UI selection)
        account_uuids:       list[str]  — explicit UUIDs
        benefit_plan_id:     int        — all accounts in this benefit plan
        verification_status: int        — filter by status (default: PENDING)
        fsp_type:            str        — 'BANK' or 'MOBILE'
        rerun:               bool       — if True, also include FAILED accounts
    """
    from tasaf_payment.models import PaymentAccount, VerificationStatus

    qs = PaymentAccount.objects.filter(is_deleted=False)

    if filters.get('account_ids'):
        return qs.filter(id__in=filters['account_ids'])

    if filters.get('account_uuids'):
        return qs.filter(uuid__in=filters['account_uuids'])

    # Default: PENDING (and optionally FAILED) accounts
    statuses = [VerificationStatus.PENDING]
    if filters.get('rerun'):
        statuses.append(VerificationStatus.FAILED)

    qs = qs.filter(verification_status__in=statuses)

    if filters.get('benefit_plan_id'):
        qs = qs.filter(
            group_beneficiary__benefit_plan_id=filters['benefit_plan_id']
        )

    if filters.get('fsp_type'):
        qs = qs.filter(fsp_type=filters['fsp_type'])

    return qs


# ─── Batch verification dispatch ──────────────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name='tasaf_payment.run_batch_verification_task',
)
def run_batch_verification_task(self, filters: dict, user_id: int):
    """
    Entry-point for large-scale MUSE verification dispatch.

    Accepts *filters* (see _build_account_queryset) and fans out chunks to
    dispatch_verification_chunk workers. Each chunk marks accounts as
    PENDING_MUSE and publishes verification requests to GovESB.
    """
    try:
        qs = _build_account_queryset(filters)
        account_ids = list(qs.values_list('id', flat=True))
        total = len(account_ids)

        if total == 0:
            logger.info("run_batch_verification_task: no accounts match filters %s", filters)
            return {'queued': 0}

        chunks = list(_chunks(account_ids, _CHUNK_SIZE))
        logger.info(
            "run_batch_verification_task: %d accounts → %d chunks (user=%s, filters=%s)",
            total, len(chunks), user_id, filters,
        )

        for i, chunk in enumerate(chunks, start=1):
            dispatch_verification_chunk.delay(chunk, user_id, chunk_index=i)

        return {'queued': total, 'chunks': len(chunks)}

    except Exception as exc:
        logger.exception("run_batch_verification_task failed: filters=%s", filters)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name='tasaf_payment.dispatch_verification_chunk',
)
def dispatch_verification_chunk(self, account_ids: list, user_id: int, chunk_index: int = 0):
    """
    Mark a chunk of PaymentAccounts as PENDING_MUSE and publish to GovESB.

    Results will be received asynchronously via MuseVerificationInboundService.
    """
    try:
        from core.models import User
        from tasaf_payment.services import MuseVerificationDispatchService

        user = User.objects.get(id=user_id)
        service = MuseVerificationDispatchService(user)

        result = service.dispatch(account_ids)
        logger.info(
            "dispatch_verification_chunk: chunk=%d dispatched %d accounts",
            chunk_index, result.get('count', 0),
        )
        return result

    except Exception as exc:
        logger.exception("dispatch_verification_chunk failed: chunk=%d", chunk_index)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=300,
    name='tasaf_payment.resubmit_failed_accounts_task',
)
def resubmit_failed_accounts_task(self, account_ids: list, user_id: int):
    """
    Resubmit FAILED accounts to MUSE after account data has been corrected.

    Resets status to PENDING then fans out to dispatch_verification_chunk.
    Only dispatches accounts that are actually in FAILED state.
    """
    try:
        from tasaf_payment.models import PaymentAccount, VerificationStatus

        ids_to_reset = list(
            PaymentAccount.objects.filter(
                id__in=account_ids,
                verification_status=VerificationStatus.FAILED,
                is_deleted=False,
            ).values_list('id', flat=True)
        )

        if not ids_to_reset:
            logger.info("resubmit_failed_accounts_task: no FAILED accounts found")
            return {'resubmitted': 0}

        PaymentAccount.objects.filter(id__in=ids_to_reset).update(
            verification_status=VerificationStatus.PENDING,
        )
        logger.info(
            "resubmit_failed_accounts_task: reset %d accounts to PENDING",
            len(ids_to_reset),
        )

        for chunk in _chunks(ids_to_reset, _CHUNK_SIZE):
            dispatch_verification_chunk.delay(chunk, user_id)

        return {'resubmitted': len(ids_to_reset)}

    except Exception as exc:
        logger.exception("resubmit_failed_accounts_task failed")
        raise self.retry(exc=exc)


# ─── Pre-audit batch ──────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name='tasaf_payment.run_batch_pre_audit_task',
)
def run_batch_pre_audit_task(self, account_ids: list, user_id: int):
    """
    Run pre-audit checks on a large set of accounts via Celery.

    Fans out to pre_audit_chunk workers.
    """
    try:
        chunks = list(_chunks(account_ids, _CHUNK_SIZE))
        logger.info(
            "run_batch_pre_audit_task: %d accounts → %d chunks (user=%s)",
            len(account_ids), len(chunks), user_id,
        )
        for i, chunk in enumerate(chunks, start=1):
            pre_audit_chunk.delay(chunk, user_id, chunk_index=i)

        return {'queued': len(account_ids), 'chunks': len(chunks)}

    except Exception as exc:
        logger.exception("run_batch_pre_audit_task failed")
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name='tasaf_payment.pre_audit_chunk',
)
def pre_audit_chunk(self, account_ids: list, user_id: int, chunk_index: int = 0):
    """Run PreAuditService on a chunk of account IDs."""
    try:
        from core.models import User
        from tasaf_payment.services import PreAuditService

        user = User.objects.get(id=user_id)
        result = PreAuditService(user).run_pre_audit(account_ids)
        logger.info(
            "pre_audit_chunk: chunk=%d passed=%d failed=%d",
            chunk_index, result.get('passed', 0), result.get('failed', 0),
        )
        return result

    except Exception as exc:
        logger.exception("pre_audit_chunk failed: chunk=%d", chunk_index)
        raise self.retry(exc=exc)
