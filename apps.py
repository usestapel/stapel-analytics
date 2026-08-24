from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    name = "stapel_analytics"
    label = "analytics"
    verbose_name = "Analytics: event registry, ingest, funnels and adapter fan-out"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects, one module each.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # The fan-out consumer plus the host's comm bridge: one subscription
        # per Action this deployment turns into an analytics event. The set
        # comes from settings and imports only — no database is touched
        # here, which is what makes it legal at ready() time (house law
        # §49). Idempotent: re-entry (tests, autoreload) adds nothing.
        from . import actions

        actions.wire_comm_bridge()

        # The erasure protocol. One call gives this module the three
        # handlers every owner library used to hand-write; what stays ours
        # is erase_subject (erasure.py). Registered from DAY ONE because
        # analytics rows are user data — a module that ships the ingest
        # before the erasure has built a personal-data store with no exit.
        from stapel_core.gdpr import register_gdpr_owner

        from .erasure import SUBJECT_TYPES
        from .gdpr import erase_subject

        register_gdpr_owner("analytics", SUBJECT_TYPES, erase_subject)
