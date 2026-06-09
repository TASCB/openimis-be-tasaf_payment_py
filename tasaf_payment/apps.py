import os

from django.apps import AppConfig

MODULE_NAME = 'tasaf_payment'

DEFAULT_CONFIG = {
    # GraphQL permissions (format: MMEEAA — module 15, entity, action)
    # Entity 20: PaymentAccount CRUD
    "gql_payment_account_search_perms": ["152001"],
    "gql_payment_account_create_perms": ["152002"],
    "gql_payment_account_update_perms": ["152003"],
    "gql_payment_account_delete_perms": ["152004"],
    # Entity 21: Verification workflow
    "gql_run_verification_perms":        ["152101"],
    "gql_approve_account_perms":         ["152102"],
    "gql_resubmit_failed_perms":         ["152103"],
    # Entity 22: Pre-audit
    "gql_run_pre_audit_perms":           ["152201"],
    # Entity 23: Paylist
    "gql_paylist_search_perms":          ["152301"],
    "gql_generate_paylist_perms":        ["152302"],
    "gql_approve_paylist_perms":         ["152303"],
    "gql_submit_paylist_perms":          ["152304"],
    # Entity 24: Return feedback
    "gql_return_feedback_search_perms":  ["152401"],
    # Entity 25: Dashboard
    "gql_dashboard_perms":               ["152501"],
    # Entity 26: Muse verification records
    "gql_muse_verification_search_perms": ["152601"],

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
