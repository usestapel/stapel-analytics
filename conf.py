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
- **Secrets** — the ``GOOGLE_ADS_*`` credentials (``conversions.py``) and
  ``CONVERSION_FEED_TOKEN`` (``feed.py``).
  These are values, not code names, so the environment step stays OPEN for
  them and that is the point: a refresh token belongs in the environment
  (or a vault that populates it), never in a settings file baked into an
  image. They ship empty, and the uploader answers ``not_configured``
  rather than failing inside the vendor SDK.

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

    # ── Google Ads offline conversions (conversions.py) ──────────────
    # Credentials. These are the ONE group here that is meant to arrive
    # from the environment, and doing so is consistent with the house rule
    # rather than an exception to it: the rule closes the env door for keys
    # that NAME CODE (`import_strings`), because a stray export must not
    # decide which module the process loads. A refresh token decides
    # nothing about control flow — it is a value, it is a secret, and a
    # secret's home is the environment (or a vault that populates it),
    # never a settings file in the image. All empty by default: the
    # uploader answers `skipped` / `not_configured` until a deployment
    # fills them, rather than failing deep inside the SDK.
    "GOOGLE_ADS_DEVELOPER_TOKEN": "",
    "GOOGLE_ADS_CLIENT_ID": "",
    "GOOGLE_ADS_CLIENT_SECRET": "",
    "GOOGLE_ADS_REFRESH_TOKEN": "",
    # Only needed when the authenticated account is a manager (MCC).
    # Absent from the required set for that reason: demanding it from a
    # direct advertiser would make a correct configuration look broken.
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "",
    # The advertiser account the conversions are written to. Dashes are
    # accepted (that is how Google prints it) and stripped before the call.
    "GOOGLE_ADS_CUSTOMER_ID": "",
    # Google Ads API version to pin, e.g. "v18". None = whatever the
    # installed SDK defaults to. A version is a compatibility decision a
    # deployment makes on its own cadence, so it is a setting rather than a
    # constant that a patch release of this library would move under it.
    "GOOGLE_ADS_API_VERSION": None,
    # Google refuses a conversion whose CLICK is older than this. Enforced
    # locally against `clicked_at` when the caller supplies it, and against
    # `conversion_at` otherwise — a strictly weaker test, and conversions.py
    # says so instead of pretending the fallback is the real rule.
    "GOOGLE_ADS_CONVERSION_WINDOW_DAYS": 90,
    # Backoff for an upload that could not be ATTEMPTED (transport, quota,
    # an outage) — never for a verdict: Google saying no is terminal.
    "GOOGLE_ADS_RETRY_BASE_SECONDS": 300,
    "GOOGLE_ADS_RETRY_MAX_SECONDS": 86400,
    # Attempts before a row is given up on and marked `rejected` with
    # `max_attempts`. A retry queue with no floor is a queue that hides a
    # permanently wrong credential behind ever-longer silences.
    "GOOGLE_ADS_MAX_ATTEMPTS": 8,
    # Beat cadence for the outbox drain
    # (`stapel_analytics.tasks.upload_click_conversions`), same crontab-kwargs
    # shape as PURGE_SCHEDULE.
    #
    # Every quarter hour, and the number is a quota decision rather than a
    # latency one. Offline conversions are not real-time by construction —
    # they are reported against a 90-day window — so nothing is bought by
    # running this every minute, and something is spent: one conversion is
    # one Google Ads API operation, and a Basic-access developer token gets
    # 15,000 of them a day for the WHOLE deployment. At the task's default
    # limit of 100 rows a pass, */15 tops out around 9,600 uploads a day,
    # which leaves the rest of the token's budget for everything else the
    # host does with it. An idle pass costs Google nothing at all: `due()`
    # is a database query and no API call happens when nothing is pending.
    #
    # It also sits comfortably above GOOGLE_ADS_RETRY_BASE_SECONDS (300), so
    # the sweep never races a row's own backoff and re-attempts it early.
    "CONVERSION_UPLOAD_SCHEDULE": {"minute": "*/15"},

    # ── The attribution cookie (attribution.py, middleware.py) ───────
    # The advertising cookie a marketing site leaves on the apex domain, so
    # that every request the browser makes to the application carries the
    # click that paid for the visit. Merged over these defaults one level
    # deep (FIELDS too), so a host that only names the cookie keeps the
    # shipped envelope.
    #
    # NAME IS EMPTY AND THAT DISABLES CAPTURE. The cookie is somebody else's
    # artefact: its name, its envelope and its consent rule belong to the
    # site that writes it, and a library that guessed a name would either
    # find nothing or decode a cookie it was never told about. The host
    # names it; until then the middleware is a no-op per request.
    #
    # * NAME        — the cookie to read. Empty = capture off.
    # * FORMAT      — the envelope. "base64url_json" is the one this release
    #                 decodes: base64url (padding optional) of a JSON object.
    # * FIELDS      — where the three values live inside that object:
    #                 {"id": <click id key>, "type": <platform key>,
    #                  "ts": <unix seconds key>}.
    # * FIRST_TOUCH — True (default): a stored attribution is never
    #                 overwritten by the cookie. False: the newest click by
    #                 `clicked_at` wins. A DECISION, and attribution.py says
    #                 why the default is the narrower door.
    # * URL_PARAM   — a query parameter whose presence means the request
    #                 already carries an explicit attribution (the frontend
    #                 passing it on a registration or an OAuth authorize);
    #                 the cookie stands down for that request. Empty = never.
    "ATTRIBUTION_COOKIE": {
        "NAME": "",
        "FORMAT": "base64url_json",
        "FIELDS": {"id": "id", "type": "type", "ts": "ts"},
        "FIRST_TOUCH": True,
        "URL_PARAM": "click_id",
    },

    # ── The conversion feed (feed.py) ────────────────────────────────
    # Bearer/query token the feed endpoint demands. A SECRET, so the
    # environment door stays open for the same reason the credentials
    # above keep it open — it is a value, not a name that decides which
    # code runs. EMPTY BY DEFAULT AND THAT DISABLES THE ENDPOINT: a feed
    # is the outbox readable over HTTP, and a library that shipped it
    # open would publish one deployment's conversion values to anybody
    # who guessed the path. Empty = 404, not "open".
    "CONVERSION_FEED_TOKEN": "",
    # The username the feed's `Authorization: Basic` door insists on. Empty
    # = any username: the password is the secret, and the connector that
    # needs this door (Google's data manager: URL, username, password, no
    # other field) makes the username mandatory to type but meaningless to
    # us. Set it when a host wants the pair pinned; compared in constant
    # time like the token.
    "CONVERSION_FEED_USERNAME": "",
    # How far back the feed looks, in days. Larger than the 90-day click
    # window on purpose: the puller decides its own schedule, and a feed
    # that dropped a row the moment its own retention said so would make
    # a missed fetch a permanently lost conversion. Rows are still gated
    # by the click window (GOOGLE_ADS_CONVERSION_WINDOW_DAYS) — this only
    # bounds how much history one response carries.
    "CONVERSION_FEED_WINDOW_DAYS": 120,
    # The `Conversion Name` column. It must match the conversion action's
    # DISPLAY NAME in the ads account character for character — the file
    # import matches on the name, not on the resource name, and a mismatch
    # is not an error, it is a file that imports zero rows. The default is
    # a placeholder every deployment is expected to replace;
    # `analytics.W012` says so when the feed is on and this was not set.
    "CONVERSION_FEED_CONVERSION_NAME": "Offline conversion",

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
