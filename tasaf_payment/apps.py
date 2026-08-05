import os

from django.apps import AppConfig

MODULE_NAME = 'tasaf_payment'

DEFAULT_CONFIG = {
    # GraphQL permissions (format: MMEEAA — module 27, entity, action)
    # Entity 00: PaymentAccount CRUD
    "gql_payment_account_search_perms": ["270001"],
    "gql_payment_account_create_perms": ["270002"],
    "gql_payment_account_update_perms": ["270003"],
    "gql_payment_account_delete_perms": ["270004"],
    # Entity 01: Verification workflow
    "gql_run_verification_perms":        ["270101"],
    "gql_approve_account_perms":         ["270102"],
    "gql_resubmit_failed_perms":         ["270103"],
    # Entity 02: Pre-audit
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
    # Entity 06: Muse verification records
    "gql_muse_verification_search_perms": ["270601"],

    # Business rules (editable via Django Admin → ModuleConfig)
    "max_resubmissions": 3,
    # MUSE accepts at most this many transactions per disbursement batch.
    # BANK and MNO are batched separately (never mixed); each FSP's eligible
    # accounts are split into Paylists of at most this size. 0 / None = no cap.
    "paylist_max_batch_size": 50000,
    # When a payroll has more than this many ACCEPTED benefits, paylist
    # generation is handed to the generate_paylists_task Celery task so the
    # request returns immediately (falls back to inline if no broker).
    # 0 / None = always run inline.
    "paylist_async_threshold": 20000,

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
    gql_resubmit_failed_perms = None
    gql_run_pre_audit_perms = None
    gql_paylist_search_perms = None
    gql_generate_paylist_perms = None
    gql_approve_paylist_perms = None
    gql_submit_paylist_perms = None
    gql_return_feedback_search_perms = None
    gql_dashboard_perms = None
    gql_muse_verification_search_perms = None

    max_resubmissions = None
    paylist_max_batch_size = None
    paylist_async_threshold = None
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
