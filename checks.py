"""Django system checks for stapel-analytics configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily.

Every check here describes a configuration that LOOKS like a working one. An
event store whose table was never installed, a registry nobody filled, a PII
guard somebody switched off in a dev branch, a funnel whose steps no event
can ever match, an unbounded retention on personal data, an erasure owner
the host never declared, a fan-out adapter enabled with nowhere to send —
all of them boot cleanly, serve 202s, and are wrong. That is precisely the
class of defect a check exists for.

No check reads the database at import; the one that queries does it lazily,
inside the check function, and swallows database errors — a check that
explodes on a fresh install replaces a useful warning with a broken boot.
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_event_store_installed(app_configs, **kwargs):
    """E001 — the default event store's app is not installed.

    The analytics stream routes to ``PostgresEventStore``, whose rows live
    in ``stapel_core.django.eventstore``'s table. Without that app in
    ``INSTALLED_APPS`` there is no table, so every batch raises — while the
    endpoint, the registry and the funnels all look perfectly healthy.
    """
    from django.apps import apps

    from .store import stream_name, uses_default_backend

    if not uses_default_backend():
        return []
    if apps.is_installed("stapel_core.django.eventstore"):
        return []
    return [checks.Error(
        f"the analytics stream {stream_name()!r} uses the default event store, "
        "but 'stapel_core.django.eventstore' is not in INSTALLED_APPS — every "
        "ingest batch will fail on a missing table.",
        hint="INSTALLED_APPS = [..., 'stapel_core.django.eventstore', "
             "'stapel_analytics'], then run migrate. Or route the stream to "
             "another backend with STAPEL_EVENTSTORE['ROUTES'].",
        id="analytics.E001",
    )]


@checks.register(checks.Tags.compatibility)
def check_events_file(app_configs, **kwargs):
    """E002 — ``EVENTS_FILE`` names a file that cannot be read or parsed.

    The registry the deployment believes it has and does not is the whole
    failure mode the declare-don't-scatter rule exists to prevent, and it is
    invisible: every event simply lands marked ``unregistered``.
    """
    from django.core.exceptions import ImproperlyConfigured

    from .conf import analytics_settings
    from .registry import _events_from_file

    if not analytics_settings.EVENTS_FILE:
        return []
    try:
        _events_from_file()
    except ImproperlyConfigured as exc:
        return [checks.Error(
            str(exc),
            hint="Point STAPEL_ANALYTICS['EVENTS_FILE'] at the analytics/"
                 "events.json your frontend's `gen:events` produces, or drop "
                 "the key and declare events in STAPEL_ANALYTICS['EVENTS'].",
            id="analytics.E002",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_registry_declared(app_configs, **kwargs):
    """W001 — nothing but the built-ins is declared.

    A deployment in this state accepts every event and marks every one of
    them unregistered, so the registry-as-contract (analytics-standard §1.1)
    is switched off in practice while looking switched on.
    """
    from .registry import declared_events, registry_mode

    if registry_mode() == "off" or declared_events():
        return []
    return [checks.Warning(
        "the analytics event registry declares nothing beyond the built-ins — "
        "every track() will be stored as 'unregistered'.",
        hint="Point STAPEL_ANALYTICS['EVENTS_FILE'] at the project's "
             "analytics/events.json, or fill STAPEL_ANALYTICS['EVENTS'].",
        id="analytics.W001",
    )]


@checks.register(checks.Tags.security)
def check_pii_guard(app_configs, **kwargs):
    """W002 — the PII guard is off.

    ``PII_MODE = "off"`` stores prop values verbatim, including the ones
    that look like an email or a phone number. It is a legitimate setting
    for a deployment whose props are machine-generated and a serious one
    everywhere else, so it says so on every boot rather than sitting silent
    in a settings file somebody copied.
    """
    from .privacy import pii_mode

    mode = pii_mode()
    if mode != "off":
        return []
    return [checks.Warning(
        "STAPEL_ANALYTICS['PII_MODE'] is 'off': event properties are stored "
        "verbatim, including values that look like emails or phone numbers "
        "(analytics-standard §1.4 bans PII in props).",
        hint="Use 'reject' (the default) or 'strip' outside development.",
        id="analytics.W002",
    )]


@checks.register(checks.Tags.compatibility)
def check_registry_mode(app_configs, **kwargs):
    """W003 — event-name validation is disabled entirely."""
    from .registry import registry_mode

    if registry_mode() != "off":
        return []
    return [checks.Warning(
        "STAPEL_ANALYTICS['REGISTRY_MODE'] is 'off': no event name is checked "
        "against the registry, so a typo becomes a new event nobody notices.",
        hint="Leave it at 'warn' (stores and marks) or use 'reject'.",
        id="analytics.W003",
    )]


@checks.register(checks.Tags.compatibility)
def check_funnel_steps(app_configs, **kwargs):
    """W004 — funnels whose steps no declared event can ever match.

    Both sources are checked: the funnels the project spec declares, and the
    authored rows. A step outside the registry makes a funnel that reports
    100% to step 1 and 0% after it, forever, and looks like a product
    problem rather than a typo.
    """
    from .funnels import declared_funnels
    from .registry import is_registered, registry_mode

    if registry_mode() == "off":
        return []
    problems = {}
    for slug, spec in declared_funnels().items():
        unknown = [step for step in spec.steps if not is_registered(step)]
        if unknown:
            problems[slug] = unknown
    for slug, steps in _authored_funnel_steps():
        unknown = [step for step in steps if not is_registered(step)]
        if unknown:
            problems[slug] = unknown
    if not problems:
        return []
    named = ", ".join(f"{slug}: {steps}" for slug, steps in sorted(problems.items()))
    return [checks.Warning(
        f"funnels name steps that are not in the event registry — {named}. "
        "They can never convert past the missing step.",
        hint="Declare the events (EVENTS_FILE / EVENTS) or fix the step names.",
        id="analytics.W004",
    )]


@checks.register(checks.Tags.security)
def check_retention(app_configs, **kwargs):
    """W005 — analytics rows are kept forever.

    They are personal data (``erasure.py``), so "forever" is a decision
    somebody has to make on purpose rather than a default that happens.
    Satisfied by either horizon: this module's ``RETENTION_DAYS`` (applied
    by ``purge_analytics``) or the event store's own per-stream retention.
    """
    from stapel_core.eventstore.conf import eventstore_settings

    from .conf import analytics_settings
    from .store import stream_name

    if analytics_settings.RETENTION_DAYS:
        return []
    if (eventstore_settings.RETENTION or {}).get(stream_name()):
        return []
    return [checks.Warning(
        "no retention horizon for analytics events: STAPEL_ANALYTICS"
        "['RETENTION_DAYS'] is unset and the stream is absent from "
        "STAPEL_EVENTSTORE['RETENTION'] — behavioural rows keyed to people "
        "will be kept forever.",
        hint="Set STAPEL_ANALYTICS['RETENTION_DAYS'] and schedule "
             "stapel_analytics.tasks.purge_analytics_events, or give the "
             "stream a retention in STAPEL_EVENTSTORE.",
        id="analytics.W005",
    )]


@checks.register(checks.Tags.compatibility)
def check_adapters(app_configs, **kwargs):
    """W006 — an enabled fan-out adapter that cannot deliver.

    An adapter with no handler, an unimportable dotted path, or the built-in
    webhook adapter enabled with no URL: each one fails on the first batch,
    in a background consumer, where the only evidence is a log line.
    """
    from django.utils.module_loading import import_string

    from .adapters import active_adapters

    problems = []
    for name, spec in sorted(active_adapters().items()):
        handler = spec.get("handler")
        if not handler:
            problems.append(f"{name}: no handler")
            continue
        if isinstance(handler, str):
            try:
                import_string(handler)
            except ImportError:
                problems.append(f"{name}: handler {handler!r} cannot be imported")
                continue
        if name == "webhook" and not (spec.get("config") or {}).get("url"):
            problems.append(f"{name}: enabled with no config['url']")
    if not problems:
        return []
    return [checks.Warning(
        "analytics fan-out adapters are enabled but cannot deliver — "
        + "; ".join(problems) + ".",
        hint="Fix the adapter spec in STAPEL_ANALYTICS['ADAPTERS'], or remove "
             "it with {'<name>': None}.",
        id="analytics.W006",
    )]


@checks.register(checks.Tags.compatibility)
def check_bridge_targets(app_configs, **kwargs):
    """W007 — the comm bridge produces events the registry does not declare.

    A bridged server milestone that is not in the registry lands marked
    ``unregistered`` and cannot be a funnel step, which defeats the only
    reason the bridge exists (analytics-standard §1: server steps of the
    same funnels).
    """
    from .actions import bridged_actions
    from .registry import is_registered, registry_mode

    if registry_mode() == "off":
        return []
    unknown = sorted(
        {
            entry[0]
            for entry in bridged_actions().values()
            if not is_registered(entry[0])
        }
    )
    if not unknown:
        return []
    return [checks.Warning(
        f"STAPEL_ANALYTICS['COMM_BRIDGE'] maps host actions to events that are "
        f"not in the registry: {unknown}. They cannot be funnel steps.",
        hint="Declare them alongside the frontend events (EVENTS_FILE / EVENTS).",
        id="analytics.W007",
    )]


@checks.register(checks.Tags.compatibility)
def check_user_hash_salt(app_configs, **kwargs):
    """W008 — a salted user hash cannot join the frontend's unsalted one.

    ``@stapel/analytics`` hashes ``sha256(userId)`` in the browser. With a
    salt configured here, a bridged server step and the clicks that led to
    it get different subject keys, and every mixed funnel silently reports a
    conversion of zero past its first server step.
    """
    from .conf import analytics_settings

    if not analytics_settings.USER_HASH_SALT:
        return []
    return [checks.Warning(
        "STAPEL_ANALYTICS['USER_HASH_SALT'] is set: server-side events hash "
        "user ids differently from @stapel/analytics (which hashes unsalted), "
        "so client and server steps of one person will not join in a funnel.",
        hint="Leave it empty for cross-tier funnels, or hash user ids the same "
             "way in the frontend before calling identify().",
        id="analytics.W008",
    )]


@checks.register(checks.Tags.security)
def check_gdpr_owner_declared(app_configs, **kwargs):
    """W009 — the host never declared this module as a data owner.

    stapel-gdpr only creates an ``ErasurePart`` for owners named in
    ``DATA_OWNERS``, so an undeclared analytics module answers no erasure
    request — and the deployment is left holding a behavioural store nobody
    can be forgotten from.
    """
    from django.conf import settings

    from .erasure import OWNER

    gdpr = getattr(settings, "STAPEL_GDPR", None)
    if not gdpr:
        # No stapel-gdpr in this deployment: the provider is still
        # registered and `user.deleted` still erases. Nothing to warn about.
        return []
    owners = gdpr.get("DATA_OWNERS") or []
    names = {
        entry.get("name") if isinstance(entry, dict) else entry for entry in owners
    }
    if OWNER in names:
        return []
    return [checks.Warning(
        f"STAPEL_GDPR['DATA_OWNERS'] does not list {OWNER!r}: erasure requests "
        "will never reach this module, and its behavioural rows will survive "
        "an account's deletion.",
        hint=f"Add {OWNER!r} to STAPEL_GDPR['DATA_OWNERS'] — it claims the "
             "subject types 'account' and 'anon'.",
        id="analytics.W009",
    )]


@checks.register(checks.Tags.security)
def check_write_keys(app_configs, **kwargs):
    """W010 — write keys are required and none are configured.

    Every ingest request answers 401 and the frontend's offline buffer fills
    until it drops. The endpoint is up, the frontend is correct, and nothing
    is ever recorded.
    """
    from .conf import analytics_settings

    if not analytics_settings.REQUIRE_WRITE_KEY:
        return []
    if analytics_settings.WRITE_KEYS:
        return []
    return [checks.Warning(
        "STAPEL_ANALYTICS['REQUIRE_WRITE_KEY'] is on but WRITE_KEYS is empty — "
        "every ingest request will be refused with 401.",
        hint="Fill STAPEL_ANALYTICS['WRITE_KEYS'] = {'<key>': '<source>'} and "
             "give the key to the frontend's stapelCollectorProvider.",
        id="analytics.W010",
    )]


def _authored_funnel_steps():
    """``[(slug, steps)]`` for authored funnels; empty on any database error.

    Swallows every database error on purpose: system checks run before
    migrations on a fresh install.
    """
    from django.db import Error as DatabaseError

    from .models import Funnel

    try:
        return list(
            Funnel.objects.filter(is_active=True).values_list("slug", "steps")[:200]
        )
    except (DatabaseError, Exception):  # noqa: B014 — includes ImproperlyConfigured
        return []


@checks.register(checks.Tags.compatibility)
def check_conversion_upload_credentials(app_configs, **kwargs):
    """W011 — conversions are queuing up and nothing can upload them.

    The configuration that looks healthiest of all: the comm function
    answers, the rows are written, the command runs and reports, and not
    one conversion has ever reached Google — because a credential is
    missing and every row comes back ``not_configured``. The warning fires
    only when there is actually a backlog, so a deployment that does not
    use the uploader never sees it.
    """
    from .conversions import is_configured

    if is_configured():
        return []
    pending = _pending_conversion_uploads()
    if not pending:
        return []
    return [checks.Warning(
        f"{pending} offline click conversion(s) are queued for Google Ads and "
        "the credentials are incomplete — every upload comes back "
        "'not_configured' and the rows will expire against the 90-day window.",
        hint="Set STAPEL_ANALYTICS['GOOGLE_ADS_DEVELOPER_TOKEN'], "
             "['GOOGLE_ADS_CLIENT_ID'], ['GOOGLE_ADS_CLIENT_SECRET'], "
             "['GOOGLE_ADS_REFRESH_TOKEN'] and ['GOOGLE_ADS_CUSTOMER_ID'] "
             "(the environment is their home), then run "
             "`manage.py analytics_upload_conversions`.",
        id="analytics.W011",
    )]


def _pending_conversion_uploads() -> int:
    """Queued uploads, or 0 on a database this check cannot read.

    Same discipline as ``_authored_funnel_steps``: a check that explodes on
    a fresh install replaces a useful warning with a broken boot.
    """
    from django.db import DatabaseError

    from .models import ConversionUpload

    try:
        return ConversionUpload.objects.filter(
            status=ConversionUpload.STATUS_PENDING
        ).count()
    except (DatabaseError, Exception):  # noqa: B014 — includes ImproperlyConfigured
        return 0


__all__ = [
    "check_adapters",
    "check_bridge_targets",
    "check_conversion_upload_credentials",
    "check_event_store_installed",
    "check_events_file",
    "check_funnel_steps",
    "check_gdpr_owner_declared",
    "check_pii_guard",
    "check_registry_declared",
    "check_registry_mode",
    "check_retention",
    "check_user_hash_salt",
    "check_write_keys",
]
