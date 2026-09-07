import os

from django.apps import AppConfig

MODULE_NAME = 'tasaf_payment'

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

    "fsp_code_aliases": {
        "Vodacom M-Pesa": "MPESA",
        "M-Pesa": "MPESA",
        "Airtel Money": "AIRTELMONEY",
        "Tigo Pesa": "TIGOPESA",
        "Mixx by Yas": "TIGOPESA",
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
    "batch_inline_fallback_limit": 1000,
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
    fsp_code_aliases = {}
    batch_inline_fallback_limit = 1000
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
