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

    run_batch_pre_audit_task(filters)
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
        location_id:         int        — households in this PAA (or below it)
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

    if filters.get('location_id'):
        from tasaf_payment.services import location_descendants_q
        qs = qs.filter(location_descendants_q(filters['location_id']))

    return qs


def _build_pre_audit_queryset(filters: dict):
    """
    Build a PaymentAccount queryset of pre-audit candidates.

    Supported filters:
        account_ids:     list[int]  — explicit IDs (from UI selection)
        account_uuids:   list[str]  — explicit UUIDs
        benefit_plan_id: str        — all accounts in this benefit plan
        fsp_type:        str        — 'BANK' or 'MOBILE'
        location_id:     int        — households in this PAA (or below it)
        rerun:           bool       — if True, also re-check accounts already FAILED

    Only VERIFIED accounts are candidates. Pre-auditing an unverified account would just
    stamp it FAILED with "Account is not VERIFIED" — noise that buries the accounts a
    person actually has to act on.
    """
    from tasaf_payment.models import PaymentAccount, PreAuditStatus, VerificationStatus
    from tasaf_payment.services import location_descendants_q

    qs = PaymentAccount.objects.filter(is_deleted=False)

    if filters.get('account_ids'):
        return qs.filter(id__in=filters['account_ids'])

    if filters.get('account_uuids'):
        return qs.filter(uuid__in=filters['account_uuids'])

    statuses = [PreAuditStatus.PENDING]
    if filters.get('rerun'):
        statuses.append(PreAuditStatus.FAILED)

    qs = qs.filter(
        verification_status=VerificationStatus.VERIFIED,
        pre_audit_status__in=statuses,
    )

    if filters.get('benefit_plan_id'):
        qs = qs.filter(group_beneficiary__benefit_plan_id=filters['benefit_plan_id'])

    if filters.get('fsp_type'):
        qs = qs.filter(fsp_type=filters['fsp_type'])

    if filters.get('location_id'):
        qs = qs.filter(location_descendants_q(filters['location_id']))

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




# ─── Paylist generation (large payrolls) ──────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name='tasaf_payment.generate_paylists_task',
)
def generate_paylists_task(self, user_id, payroll_id, batch_type,
                           payment_cycle_id=None, location_id=None,
                           destination=None):
    """
    Build a payroll's Paylists (and bulk-create their items) off the request
    thread. Used for large payrolls (see PaylistService.generate dispatcher).

    Delegates to PaylistService._generate_sync, which applies the per-FSP
    50k-batch split and bulk_creates line items.
    """
    try:
        from core.models import User
        from tasaf_payment.services import PaylistService

        user = User.objects.get(id=user_id)
        result = PaylistService(user)._generate_sync(
            payroll_id, batch_type, payment_cycle_id, location_id, destination,
        )
        logger.info(
            "generate_paylists_task: payroll=%s → %s paylist(s), %s item(s)",
            payroll_id, result.get('paylist_count'), result.get('total_items'),
        )
        return result

    except Exception as exc:
        logger.exception("generate_paylists_task failed: payroll=%s", payroll_id)
        raise self.retry(exc=exc)


# ─── Pre-audit batch ──────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name='tasaf_payment.run_batch_pre_audit_task',
)
def run_batch_pre_audit_task(self, filters: dict, user_id: int):
    """
    Entry-point for large-scale pre-audit.

    Accepts *filters* (see _build_pre_audit_queryset) and fans out chunks to
    pre_audit_chunk workers. Takes filters rather than an id list so a PAA-wide run is
    never limited to whatever a searcher page had loaded.
    """
    try:
        qs = _build_pre_audit_queryset(filters)
        account_ids = list(qs.values_list('id', flat=True))
        total = len(account_ids)

        if total == 0:
            logger.info("run_batch_pre_audit_task: no accounts match filters %s", filters)
            return {'queued': 0}

        chunks = list(_chunks(account_ids, _CHUNK_SIZE))
        logger.info(
            "run_batch_pre_audit_task: %d accounts → %d chunks (user=%s, filters=%s)",
            total, len(chunks), user_id, filters,
        )
        for i, chunk in enumerate(chunks, start=1):
            pre_audit_chunk.delay(chunk, user_id, chunk_index=i)

        return {'queued': total, 'chunks': len(chunks)}

    except Exception as exc:
        logger.exception("run_batch_pre_audit_task failed: filters=%s", filters)
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
