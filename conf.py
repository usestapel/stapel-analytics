"""Settings namespace for stapel-analytics.

All configuration is read through ``analytics_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_ANALYTICS`` dict -> flat Django
setting of the same name -> environment variable -> the default below.

Three kinds of key live here, and the difference matters:

- **MERGE-registries** — ``EVENTS`` (``registry.py``) and ``ADAPTERS``
  (``adapters.py``). Built-ins <- this key <- runtime registration, last
  layer wins, a value of ``None`` REMOVES an entry. This is the seam that
  makes the event vocabulary and the fan-out targets a property of the
  deployment instead of a property of a release.
- **Dotted paths** — ``EVENT_STORE_SUBJECT``, ``PII_GUARD``,
  ``INGEST_AUTHENTICATION`` mappers, ``COMM_BRIDGE`` mappers. Everything
  that NAMES CODE is either an ``import_strings`` member or resolved with
  ``import_string`` at call time, and none of them is readable from the
  environment (``stapel_core.conf``: a name that decides which code runs is
  not read from a variable anything in the pod can set).
- **Plain values** — retention, caps, modes.

**Every switch that trades privacy for reach ships CLOSED.** Prop values
that look like PII are refused, not stored (``PII_MODE = "reject"``); the
user hash is unsalted only because the shipped ``@stapel/analytics`` facade
hashes unsalted and a salt would silently stop server-side funnel steps
joining client-side ones — the check says so either way.
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
    # ── Event registry (registry.py) ─────────────────────────────────
    # {event_name: definition | None}, merged OVER BUILTIN_EVENTS. A
    # definition is the SAME literal shape @stapel/analytics' `defineEvent`
    # projects into `analytics/events.json`
    # ({"name", "description", "props", "flow"}), so a project's generated
    # registry can be handed to this key verbatim. A list of definitions is
    # accepted too — that is what events.json actually contains.
    # None removes an entry (including a built-in one).
    "EVENTS": {},
    # Path to a project's `analytics/events.json`. Read once, lazily, and
    # merged UNDER `EVENTS` — a file is a projection of the project spec
    # (analytics-standard §4), the setting is the deployment's last word.
    # Reading a file is legal at check time and at first ingest; it never
    # happens at AppConfig.ready() (house law §49 is about the database,
    # but the same discipline keeps boot honest).
    "EVENTS_FILE": None,
    # What an unregistered event name does. "warn" (default) stores it and
    # marks the row `unregistered` — analytics-standard §3 is explicit that
    # events are never LOST to validation; "reject" refuses it at ingest;
    # "off" disables the check entirely (and says so at boot).
    "REGISTRY_MODE": "warn",

    # ── Ingest ───────────────────────────────────────────────────────
    # Events one batch may carry. A larger body is refused whole.
    "MAX_BATCH_SIZE": 500,
    # Serialized size cap of one request body, in bytes.
    "MAX_BODY_BYTES": 1048576,
    # Serialized size cap of one event's props.
    "MAX_PROPS_BYTES": 16384,
    # Longest event / page / source name accepted.
    "MAX_NAME_LENGTH": 200,
    # {write_key: source_name}. EMPTY BY DEFAULT and, with
    # REQUIRE_WRITE_KEY off, that means anonymous ingest is accepted and
    # every batch is attributed to DEFAULT_SOURCE. A deployment that wants
    # per-source attribution fills this map.
    "WRITE_KEYS": {},
    # Refuse a batch that carries no recognized write key. Ships OFF: the
    # shipped @stapel/analytics facade sends `write_key` only when a host
    # configured one, and a module that 401s a correct frontend out of the
    # box is a module nobody mounts.
    "REQUIRE_WRITE_KEY": False,
    "DEFAULT_SOURCE": "web",
    # Authentication classes for the INGEST endpoint only, as dotted paths.
    # EMPTY on purpose: the ingest identity is the `user_hash` inside the
    # payload, not a session — and an empty list is what makes
    # `navigator.sendBeacon` work, since a beacon carries no CSRF token and
    # DRF's SessionAuthentication enforces CSRF from inside authentication.
    # A host that wants token-authenticated ingest names its classes here.
    "INGEST_AUTHENTICATION": [],
    # How far ahead of the server clock a client timestamp may be before it
    # is replaced by server time. Browsers have wrong clocks; a funnel
    # ordered by a wrong clock is a funnel that reads backwards.
    "MAX_CLOCK_SKEW_SECONDS": 300,
    # How far BEHIND the server clock a client timestamp may be before the
    # event is refused as a replay of an ancient offline buffer.
    "MAX_EVENT_AGE_SECONDS": 604800,

    # ── Privacy (analytics-standard §1.4) ────────────────────────────
    # What a PII-shaped prop VALUE does. "reject" (default) refuses the
    # event and says which prop; "strip" replaces the value with
    # "[redacted]" and stores the event; "warn" stores it and logs; "off"
    # disables the guard (and says so at boot).
    "PII_MODE": "reject",
    # Salt mixed into the user id before hashing. EMPTY by default because
    # the shipped @stapel/analytics facade hashes `sha256(userId)` unsalted:
    # with a salt here, a server-side funnel step and the client-side step
    # of the same person get different subject keys and never join. Setting
    # it is a real privacy improvement and a real join break — analytics.W008
    # names the trade on every boot.
    "USER_HASH_SALT": "",
    # Longest anon/session id accepted from a client (they are opaque).
    "MAX_ID_LENGTH": 128,

    # ── Storage (stapel_core.eventstore seam) ────────────────────────
    # Stream name every analytics row is appended to. Per-stream backend
    # routing, buffering and partitioning are the event store's own
    # settings (STAPEL_EVENTSTORE) — this module never touches a backend.
    "STREAM": "analytics",
    # Raw retention in days applied by `manage.py purge_analytics` /
    # `stapel_analytics.tasks.purge_analytics_events`. None = keep forever,
    # which for rows that are personal data is a decision, so it warns.
    "RETENTION_DAYS": 400,
    "PURGE_SCHEDULE": {"hour": 4, "minute": 30},
    # Rows one report/erasure pass reads per page from the event store.
    "QUERY_PAGE_SIZE": 1000,
    # Hard ceiling on the rows a single funnel report will scan. A report
    # that would exceed it comes back `truncated`, rather than becoming an
    # unbounded table scan somebody triggers from a dashboard.
    "MAX_REPORT_EVENTS": 200000,

    # ── Funnels ──────────────────────────────────────────────────────
    # Funnels DECLARED by the project spec (analytics-standard §4):
    # {slug: {"title", "steps": [...], "window_seconds"}}. Merged UNDER the
    # rows of the Funnel table — a declared funnel is read-only over the
    # API, an authored one is editable.
    "FUNNELS": {},
    # Default conversion window, measured from a subject's FIRST step.
    "DEFAULT_FUNNEL_WINDOW_SECONDS": 604800,
    "MAX_FUNNEL_STEPS": 12,
    "MAX_FUNNELS_PER_OWNER": 50,

    # ── Server-side fan-out (adapters.py) ────────────────────────────
    # {adapter_name: spec | None}, merged OVER BUILTIN_ADAPTERS. `config`
    # merges one level deep so a host names a url without restating the
    # handler. None removes an adapter.
    "ADAPTERS": {},
    # Fan-out is driven by the `analytics.events.recorded` Action, which
    # travels the transactional outbox — delivery happens out of band, not
    # on the ingest request thread (analytics-standard §3). Turning this
    # off stops the emit entirely: the event store still records everything.
    "FANOUT_ENABLED": True,
    # Events one fan-out Action carries. Larger batches are chunked.
    "FANOUT_BATCH_SIZE": 200,

    # ── comm bridge (analytics-standard §1) ──────────────────────────
    # {action_name: event_name | {"event": ..., "props": [...],
    #  "mapper": "dotted.path"}}. A payment, an email, a webhook delivery
    # becomes a step of the same funnels as the clicks that led to it.
    # EMPTY by default: a bridge that guesses which of a host's Actions are
    # business milestones would invent a funnel nobody declared.
    "COMM_BRIDGE": {},
    # Source recorded on bridged (and `track()`-ed) server events.
    "SERVER_SOURCE": "server",

    # ── API surface ──────────────────────────────────────────────────
    "MAX_PAGE_SIZE": 100,
    # Mount the un-versioned `/<mount>/api/events` alias next to the canon
    # `/<mount>/api/v1/events`. @stapel/analytics 0.1 hardcodes the former
    # (`COLLECTOR_PATH`), and it shipped before this module existed. A
    # deployment whose frontend is on a facade that targets v1 turns this
    # off and the alias is gone.
    "LEGACY_INGEST_ALIAS": True,

    # ── Seams (dotted paths; never read from the environment) ────────
    # Decides which id a funnel groups by for one event row: user hash,
    # else anonymous id, else session id. Swap it in a deployment whose
    # notion of "the same person" is different.
    "SUBJECT_RESOLVER": "stapel_analytics.ingest.default_subject",
    # The PII heuristic itself. Same contract as the frontend guard, so a
    # host tightening one tightens the other by choice rather than by luck.
    "PII_GUARD": "stapel_analytics.privacy.looks_like_pii",
}

analytics_settings = AppSettings(
    "STAPEL_ANALYTICS",
    defaults=DEFAULTS,
    import_strings=("SUBJECT_RESOLVER", "PII_GUARD"),
)

__all__ = ["analytics_settings", "DEFAULTS"]
