import os

from django.apps import AppConfig

MODULE_NAME = 'tasaf_payment'

MUSE_REJECTION_REASON_CLASSES = {
    "MSISDN invalid": "DATA",
    "The receiver number must be a customer number": "DATA",
    "System internal error": "TRANSIENT",
}

DEFAULT_CONFIG = {
    "gql_payment_account_search_perms": ["270001"],
    "gql_payment_account_create_perms": ["270002"],
    "gql_payment_account_update_perms": ["270003"],
    "gql_payment_account_delete_perms": ["270004"],
    # Entity 01: Verification workflow
    "gql_run_verification_perms":        ["270101"],
    "gql_approve_account_perms":         ["270102"],
    # Entity 02: Pre-audit
    "gql_pre_audit_search_perms":        ["270202"],
    "gql_run_pre_audit_perms":           ["270201"],
    # Entity 03: Paylist
    "gql_paylist_search_perms":          ["270301"],
    "gql_generate_paylist_perms":        ["270302"],
    "gql_approve_paylist_perms":         ["270303"],
    "gql_submit_paylist_perms":          ["270304"],
    # Entity 04: Return feedback
    "gql_return_feedback_search_perms":  ["270401"],
    # Entity 05: Dashboard
    "gql_dashboard_perms":               ["270501"],
    # Auditor-facing reports — deliberately separate from the operational rights.
    "gql_reports_perms":                 ["270801"],
    # Entity 06: Muse verification records
    "gql_muse_verification_search_perms": ["270601"],

    "gql_withdrawal_charge_search_perms": ["270701"],
    "gql_withdrawal_charge_manage_perms": ["270702"],
    "gql_muse_settings_search_perms": ["270901"],
    "gql_muse_settings_propose_perms": ["270902"],
    "gql_muse_settings_approve_perms": ["270903"],
    "muse_change_approval_flow": "MUSE_SETTINGS_CHANGE",
    # SEND: real GovESB send. RECORD_ONLY: demo servers without GovESB — marks SUBMITTED unsent.
    "muse_submit_mode": "SEND",
    "muse_send_max_attempts": 3,
    "muse_send_backoff_seconds": 2,
    # NORMAL: request to MUSE's own End Point (apiCode). PUSH: our push End Point (pushCode).
    "muse_request_type": "NORMAL",
    # Also push TASAF's ACKs over GovESB (they are always returned in the HTTP response).
    "muse_ack_push": False,
    "muse_rejection_reason_classes": MUSE_REJECTION_REASON_CLASSES,
    "muse_transient_retry_limit": 2,
    "withdrawn_hold_grace_days": 0,
    "withdrawn_hold_days_without_cycle": 30,

    "fsp_code_aliases": {
        "Vodacom M-Pesa": "MPESA",
        "M-Pesa": "MPESA",
        "Airtel Money": "AIRTELMONEY",
        "Tigo Pesa": "TIGOPESA",
        "Mixx by Yas": "TIGOPESA",
        "MIXX": "TIGOPESA",
        "T-Pesa": "TTCL",
        "Halopesa": "HALOPESA",
        "Ezy Pesa": "EZYPESA",
        "NMB Bank": "NMB",
        "CRDB Bank": "CRDB",
        "NBC Bank": "NBC",
        "Equity Bank": "EQUITY",
        "Akiba Bank": "AKIBA",
        "Azania Bank": "AZANIA",
        "TPB Bank": "TPB",
    },
    "mobile_prefixes": {
        "MPESA": ["074", "075", "076", "079"],
        "TIGOPESA": ["065", "067", "071", "077"],
        "AIRTELMONEY": ["068", "069", "078"],
        "HALOPESA": ["061", "062"],
        "TTCL": ["073"],
        "EZYPESA": ["077"],
    },
    "sub_budget_classes": [
        {"code": 211, "description": "UN Agencies"},
        {"code": 212, "description": "Government of Tanzania"},
        {"code": 213, "description": "OPEC"},
        {"code": 214, "description": "World Bank (IDA)"},
        {"code": 215, "description": "Development Partners (DPs)"},
        {"code": 216, "description": "GOT II"},
    ],
    "batch_inline_fallback_limit": 1000,
    # Rows per GePG verification request (one FSP each; larger FSPs are split).
    "verification_batch_max_rows": 50000,
    "gepg_message_format": "XML",
    "gepg_send_max_attempts": 3,
    "gepg_send_backoff_seconds": 2,
    # GePG usually answers in minutes: remind after this, release unanswered accounts after the timeout.
    "gepg_answer_warning_minutes": 30,
    "gepg_answer_timeout_hours": 6,
    # GePG codes meaning "could not check" (provider unreachable or silent): the account goes back to
    # where it was instead of failing. Add codes here as GePG confirms them.
    "gepg_retry_codes": ["7201", "7355"],
    "require_first_payment_check": True,
    "check_mobile_prefix": True,
    "name_match": {
        "verified_min": 80,
        "manual_min": 60,
        "token_min_ratio": 0.80,
        "initial_credit": 0.5,
        "min_tokens_matched": 2,
        "require_surname": True,
        "surname_min_ratio": 0.90,
    },
    # Applied at paylist generation. Off by default: turning it on changes disbursed totals.
    "apply_withdrawal_charges": False,
    "charges_require_approval": True,

    "paylist_max_batch_size": 50000,
    "paylist_async_threshold": 20000,
    # Service account attributed to gateway callbacks (no logged-in user exists).
    "inbound_system_username": "Admin",

    # GovESB integration (TODO: to be configured next after discussion with MUSE team)
    "govesb_endpoint": os.getenv('GOVESB_ENDPOINT', ''),
    "govesb_api_key":  os.getenv('GOVESB_API_KEY', ''),
}


class TasafPaymentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    gql_payment_account_search_perms = None
    gql_payment_account_create_perms = None
    gql_payment_account_update_perms = None
    gql_payment_account_delete_perms = None
    gql_run_verification_perms = None
    gql_approve_account_perms = None
    gql_pre_audit_search_perms = None
    gql_run_pre_audit_perms = None
    gql_paylist_search_perms = None
    gql_generate_paylist_perms = None
    gql_approve_paylist_perms = None
    gql_submit_paylist_perms = None
    gql_return_feedback_search_perms = None
    gql_dashboard_perms = None
    gql_reports_perms = None
    gql_muse_verification_search_perms = None
    gql_withdrawal_charge_search_perms = None
    gql_withdrawal_charge_manage_perms = None
    gql_muse_settings_search_perms = None
    gql_muse_settings_propose_perms = None
    gql_muse_settings_approve_perms = None
    muse_change_approval_flow = None
    muse_submit_mode = None
    muse_send_max_attempts = None
    muse_send_backoff_seconds = None
    muse_request_type = None
    muse_ack_push = None
    muse_rejection_reason_classes = MUSE_REJECTION_REASON_CLASSES
    muse_transient_retry_limit = 2
    withdrawn_hold_grace_days = 0
    withdrawn_hold_days_without_cycle = 30
    fsp_code_aliases = {}
    mobile_prefixes = {}
    sub_budget_classes = []
    batch_inline_fallback_limit = 1000
    verification_batch_max_rows = 50000
    gepg_message_format = 'XML'
    gepg_send_max_attempts = 3
    gepg_send_backoff_seconds = 2
    gepg_answer_warning_minutes = 30
    gepg_answer_timeout_hours = 6
    gepg_retry_codes = ["7201", "7355"]
    require_first_payment_check = True
    check_mobile_prefix = True
    name_match = {}
    apply_withdrawal_charges = False
    charges_require_approval = True

    paylist_max_batch_size = None
    paylist_async_threshold = None
    inbound_system_username = None
    govesb_endpoint = None
    govesb_api_key = None

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)

        from tasaf_payment.signals import bind_service_signals
        bind_service_signals()

    @classmethod
    def __load_config(cls, cfg):
        for field in cfg:
            if hasattr(TasafPaymentConfig, field):
                setattr(TasafPaymentConfig, field, cfg[field])
