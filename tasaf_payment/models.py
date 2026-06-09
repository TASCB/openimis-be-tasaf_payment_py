from django.db import models
from django.utils.translation import gettext as _

from core.models import HistoryModel, HistoryBusinessModel, UUIDModel, ObjectMutation, MutationLog


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class VerificationStatus(models.IntegerChoices):
    PENDING  = 0, _("PENDING")
    VERIFIED = 1, _("VERIFIED")
    FAILED   = 2, _("FAILED")
    MANUAL   = 3, _("MANUAL")
    PENDING_MUSE = 4, _("PENDING_MUSE")   # sent to MUSE, awaiting result


class PreAuditStatus(models.TextChoices):
    PENDING = 'PENDING', _("PENDING")
    PASSED  = 'PASSED',  _("PASSED")
    FAILED  = 'FAILED',  _("FAILED")


class ActiveCheckStatus(models.TextChoices):
    PENDING  = 'PENDING',  _("PENDING")
    ACTIVE   = 'ACTIVE',   _("ACTIVE")
    INACTIVE = 'INACTIVE', _("INACTIVE")


class BatchType(models.TextChoices):
    BANK   = 'BANK',   _("BANK")
    MNO    = 'MNO',    _("MNO")
    MIXED  = 'MIXED',  _("MIXED")


class PaylistStatus(models.TextChoices):
    DRAFT            = 'DRAFT',            _("DRAFT")
    PENDING_APPROVAL = 'PENDING_APPROVAL', _("PENDING_APPROVAL")
    APPROVED         = 'APPROVED',         _("APPROVED")
    SUBMITTED        = 'SUBMITTED',        _("SUBMITTED")
    CLOSED           = 'CLOSED',           _("CLOSED")


class PaylistItemStatus(models.TextChoices):
    PENDING    = 'PENDING',    _("PENDING")
    PROCESSED  = 'PROCESSED',  _("PROCESSED")
    RETURNED   = 'RETURNED',   _("RETURNED")
    UNAPPLIED  = 'UNAPPLIED',  _("UNAPPLIED")


class MuseVerificationResult(models.TextChoices):
    PASSED = 'PASSED', _("PASSED")
    FAILED = 'FAILED', _("FAILED")
    MANUAL = 'MANUAL', _("MANUAL")


class MuseVerificationType(models.TextChoices):
    MOBILE_VALIDATION = 'MOBILE_VALIDATION', _("MOBILE_VALIDATION")
    FSP_ACCOUNT       = 'FSP_ACCOUNT',       _("FSP_ACCOUNT")
    ACTIVE_CHECK      = 'ACTIVE_CHECK',       _("ACTIVE_CHECK")


class ReturnFeedbackType(models.TextChoices):
    UNAPPLIED = 'UNAPPLIED', _("UNAPPLIED")
    RETURNED  = 'RETURNED',  _("RETURNED")
    PARTIAL   = 'PARTIAL',   _("PARTIAL")


# ---------------------------------------------------------------------------
# PaymentAccount
# ---------------------------------------------------------------------------

class PaymentAccount(HistoryBusinessModel):
    """
    Bank or mobile money account for a GroupBeneficiary.

    FK to social_protection.GroupBeneficiary links the account to a household
    enrolled in a benefit plan. One household may have multiple accounts across
    FSPs, but only one should be marked is_primary=True at a time.

    verification_status reflects the MUSE-returned result (not internal scoring).
    pre_audit_status and active_check_status are TASAF MIS workflow states.
    muse_verification_reference is the MUSE reference ID for the last verification.

    Cross-module FK uses DO_NOTHING so removing a beneficiary does not cascade.
    """
    group_beneficiary = models.ForeignKey(
        'social_protection.GroupBeneficiary',
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        related_name='payment_accounts',
    )
    account_number = models.CharField(max_length=50)
    account_name = models.CharField(max_length=255, blank=True, null=True)
    fsp_type = models.CharField(max_length=20)    # BANK or MOBILE
    fsp_name = models.CharField(max_length=100)
    verification_status = models.IntegerField(
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
    )
    # MUSE returns pass/fail + reason — no internal numeric score
    muse_verification_reference = models.CharField(max_length=100, null=True, blank=True)
    pre_audit_status = models.CharField(
        max_length=20,
        choices=PreAuditStatus.choices,
        default=PreAuditStatus.PENDING,
    )
    active_check_status = models.CharField(
        max_length=20,
        choices=ActiveCheckStatus.choices,
        default=ActiveCheckStatus.PENDING,
    )
    is_primary = models.BooleanField(default=True)
    json_ext = models.JSONField(db_column='Json_ext', blank=True, default=dict)

    class Meta:
        managed = True
        db_table = 'tasaf_PaymentAccount'
        indexes = [
            models.Index(fields=['account_number'], name='tasaf_pa_accno_idx'),
            models.Index(fields=['verification_status'], name='tasaf_pa_vstatus_idx'),
            models.Index(fields=['fsp_type', 'fsp_name'], name='tasaf_pa_fsp_idx'),
            models.Index(fields=['pre_audit_status'], name='tasaf_pa_preaudit_idx'),
            # Payroll-creation guard EXISTS subquery (group_beneficiary + verified primary).
            models.Index(
                fields=['group_beneficiary', 'verification_status', 'is_primary', 'is_deleted'],
                name='tasaf_pa_guard_idx',
            ),
        ]

    def __str__(self):
        return f"{self.account_number} ({self.fsp_name}) [{self.get_verification_status_display()}]"

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()
        if user.is_imis_admin:
            return queryset
        if user.is_anonymous:
            return queryset.filter(id=-1)
        return queryset


# ---------------------------------------------------------------------------
# MuseVerificationRecord  (replaces VerificationRecord)
# ---------------------------------------------------------------------------

class MuseVerificationRecord(HistoryModel):
    """
    Immutable result record pushed from MUSE via GovESB.

    One record per verification event per PaymentAccount. Multiple records may
    exist for the same account across different verification rounds or types.

    """
    payment_account = models.ForeignKey(
        PaymentAccount,
        on_delete=models.DO_NOTHING,
        related_name='muse_verification_records',
    )
    muse_reference = models.CharField(max_length=100, null=True, blank=True)
    verification_type = models.CharField(
        max_length=30,
        choices=MuseVerificationType.choices,
        default=MuseVerificationType.FSP_ACCOUNT,
    )
    result = models.CharField(
        max_length=10,
        choices=MuseVerificationResult.choices,
    )
    failure_reason = models.TextField(null=True, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'tasaf_MuseVerificationRecord'
        indexes = [
            models.Index(fields=['payment_account', 'received_at'],
                         name='tasaf_muse_vr_account_idx'),
        ]

    def __str__(self):
        return f"MuseVerificationRecord [{self.verification_type}] {self.result} ref={self.muse_reference}"


class VerificationRecord(HistoryModel):
    """
    NIDA-based verification record. No longer depended on it is left for future use
    """
    payment_account = models.ForeignKey(
        PaymentAccount,
        on_delete=models.DO_NOTHING,
        related_name='verification_records',
    )
    nida_name = models.CharField(max_length=255, blank=True, null=True)
    fsp_name_returned = models.CharField(max_length=255, blank=True, null=True)
    match_score = models.IntegerField(null=True, blank=True)
    routing_decision = models.CharField(max_length=20, null=True, blank=True)
    run_reference = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        managed = True
        db_table = 'tasaf_VerificationRecord'

    def __str__(self):
        return f"VerificationRecord (legacy) {self.run_reference} score={self.match_score}"


# ---------------------------------------------------------------------------
# Paylist
# ---------------------------------------------------------------------------

class Paylist(HistoryBusinessModel):
    """
    TASAF-specific payment dispatch batch.

    One payroll may produce multiple paylists (e.g., one Bank + one MNO).
    A paylist is what gets submitted to MUSE for payment — not the raw payroll.
    The underlying payroll handles accounting; the paylist handles dispatch.
    """
    payroll = models.ForeignKey(
        'payroll.Payroll',
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        related_name='paylists',
    )
    payment_cycle = models.ForeignKey(
        'payment_cycle.PaymentCycle',
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        related_name='paylists',
    )
    batch_type = models.CharField(
        max_length=10,
        choices=BatchType.choices,
    )
    status = models.CharField(
        max_length=20,
        choices=PaylistStatus.choices,
        default=PaylistStatus.DRAFT,
    )
    location = models.ForeignKey(
        'location.Location',
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        related_name='paylists',
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    muse_batch_reference = models.CharField(max_length=100, null=True, blank=True)
    # When a payroll's eligible accounts for one FSP exceed the MUSE batch size,
    # generation splits them into several sibling Paylists. They share a
    # batch_group (UUID) and carry their 1-based position (batch_sequence) within
    # the group of batch_total siblings. Single-batch generation → seq 1 / total 1.
    batch_group = models.UUIDField(null=True, blank=True, db_index=True)
    batch_sequence = models.IntegerField(null=True, blank=True)
    batch_total = models.IntegerField(null=True, blank=True)
    json_ext = models.JSONField(db_column='Json_ext', blank=True, default=dict)

    class Meta:
        managed = True
        db_table = 'tasaf_Paylist'
        indexes = [
            models.Index(fields=['status'], name='tasaf_paylist_status_idx'),
            models.Index(fields=['batch_type'], name='tasaf_paylist_btype_idx'),
        ]

    def __str__(self):
        return f"Paylist [{self.batch_type}] {self.status}"


class PaylistItem(HistoryModel):
    """
    One benefit line item within a Paylist.

    Links the paylist to a specific BenefitConsumption and PaymentAccount.
    Tracks per-item MUSE dispatch results and return feedback.
    """
    paylist = models.ForeignKey(
        Paylist,
        on_delete=models.DO_NOTHING,
        related_name='items',
    )
    payment_account = models.ForeignKey(
        PaymentAccount,
        on_delete=models.DO_NOTHING,
        related_name='paylist_items',
    )
    benefit_consumption = models.ForeignKey(
        'payroll.BenefitConsumption',
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        related_name='paylist_items',
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=PaylistItemStatus.choices,
        default=PaylistItemStatus.PENDING,
    )
    muse_reference = models.CharField(max_length=100, null=True, blank=True)
    return_reason = models.TextField(null=True, blank=True)
    final_status = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        managed = True
        db_table = 'tasaf_PaylistItem'
        indexes = [
            models.Index(fields=['paylist', 'status'], name='tasaf_pli_status_idx'),
        ]

    def __str__(self):
        return f"PaylistItem {self.status} amount={self.amount}"


# ---------------------------------------------------------------------------
# ReturnFeedback
# ---------------------------------------------------------------------------

class ReturnFeedback(HistoryModel):
    """
    Return or unapplied notification received from MUSE via GovESB.

    One record per feedback event per PaylistItem.
    """
    paylist_item = models.ForeignKey(
        PaylistItem,
        on_delete=models.DO_NOTHING,
        related_name='return_feedbacks',
    )
    feedback_type = models.CharField(
        max_length=20,
        choices=ReturnFeedbackType.choices,
    )
    reason_code = models.CharField(max_length=50, null=True, blank=True)
    reason_description = models.TextField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'tasaf_ReturnFeedback'

    def __str__(self):
        return f"ReturnFeedback [{self.feedback_type}] {self.reason_code}"


# ---------------------------------------------------------------------------
# PaymentAccountMutation  (journaling — unchanged)
# ---------------------------------------------------------------------------

class PaymentAccountMutation(UUIDModel, ObjectMutation):
    """Links a PaymentAccount to a MutationLog for journaling."""
    payment_account = models.ForeignKey(
        PaymentAccount,
        models.DO_NOTHING,
        related_name='mutations',
    )
    mutation = models.ForeignKey(
        MutationLog,
        models.DO_NOTHING,
        related_name='tasaf_payment',
    )

    class Meta:
        managed = True
        db_table = 'tasaf_PaymentAccountMutation'
