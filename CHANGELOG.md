# Changelog

All notable changes to stapel-analytics are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

## [0.1.0] — 2026-08-24

First release. The backend half of the analytics standard
(`docs/pending/analytics-standard-v2.md`); the frontend half shipped earlier
as `@stapel/analytics` (`docs/done/analytics-standard-v1.md`).

### Added

- **The event registry** (`registry.py`) with the fleet's merge semantics:
  built-ins ← `EVENTS_FILE` ← `STAPEL_ANALYTICS["EVENTS"]` ←
  `register_event()`, a definition of `None` removing an entry. Definitions
  are the same literal shape `@stapel/analytics`' `defineEvent` projects into
  `analytics/events.json`, so a project's generated registry is the
  backend's registry with no hand-maintained copy in between. Names may be
  prefix patterns (`flow.*`), an exact name beating a pattern. Validation is
  a WARNING by default — an unregistered event is stored and marked, never
  dropped.

- **Ingest** — `POST /analytics/api/v1/events`, accepting the batch
  `@stapel/analytics`' `stapelCollectorProvider` already sends
  (`{events: [{id, kind, name, props, userHash, ts}], write_key}`, `ts` in
  epoch milliseconds). Both camelCase and snake_case spellings are read;
  `anon_id` / `session_id` are accepted at batch and event level for
  producers that have them. Anonymous and CSRF-free by default so
  `navigator.sendBeacon` works; write keys name the source. Answers **202
  with a receipt** — partial acceptance, because the facade retries a batch
  until it drops all of it.

- **The legacy ingest alias** `/analytics/api/events`, the un-versioned path
  the shipped facade hardcodes. Switchable (`LEGACY_INGEST_ALIAS`) and
  documented as a compatibility surface with an end date.

- **The PII guard** (`privacy.py`), refusing rather than warning by default:
  prop VALUES that look like an email or a phone number refuse the event and
  the receipt names the prop. Same heuristics as the frontend's `pii.ts`,
  with one one-directional refinement (an ISO-8601 timestamp matches the
  phone shape and is exempted here).

- **Storage through `stapel_core.eventstore`** (`store.py`) — the fleet's
  append-only stream primitive, which is already the design's partitionable
  table with configurable retention. This module owns no event table, and
  exactly one file imports the seam.

- **Funnels** (`funnels.py`, `Funnel`): conversion by step over a window
  measured from a subject's first step, with an equal-length comparison
  period. Definitions merge authored rows OVER the funnels a project spec
  declares (`FUNNELS`), which are read-only over the API. Steps are
  validated against the registry at authoring time. Reports are bounded and
  report their own truncation.

- **The comm bridge** (`actions.py`): `COMM_BRIDGE` maps a host's Actions to
  analytics events, so a payment or an email becomes a step of the same
  funnel as the clicks that led to it. `user_id` is hashed exactly the way
  the browser hashes it — that unsalted equality is what makes the
  cross-tier join work. A prop allowlist or a dotted-path mapper shapes the
  payload; a broken mapper raises at `ready()`.

- **Server-side fan-out** (`adapters.py`): an open merge-registry of
  adapters with `webhook` and `log` built in (both disabled — one has no URL
  and the other would put guarded data in a log aggregator). Delivery rides
  the `analytics.events.recorded` Action through comm's transactional
  outbox, never the ingest request thread. A failing adapter is contained;
  `manage.py analytics_fanout` replays a range.

- **Erasure from day one** (`erasure.py`, `gdpr.py`): analytics rows are
  user data. Owner `analytics`, subject types `account` and `anon`, hard
  delete (an analytics row without its subject cannot be attributed,
  funnelled or reported on). Erasing an account also purges the anonymous
  sessions it was ever seen under, collected before the first pass. Both
  protocols are wired: the 0.5.0 bus request via
  `stapel_core.gdpr.register_gdpr_owner`, and the in-process
  `AnalyticsGDPRProvider` for the export archive.

- **comm surface**: Functions `analytics.track` (the server-side `track()`
  of the design), `analytics.event_registry` (what a Studio verifier reads
  to compare the declared registry with the calls the code makes) and
  `analytics.funnel_report`. Emits `analytics.events.recorded`.

- **Twelve system checks**, each describing a configuration that looks like
  a working one: the event store's app missing (`E001`), an unreadable
  events file (`E002`), an undeclared vocabulary (`W001`), the PII guard or
  the registry check switched off (`W002`/`W003`), funnels naming steps
  nothing can emit (`W004`), no retention horizon on personal data
  (`W005`), an enabled adapter that cannot deliver (`W006`), bridge targets
  outside the registry (`W007`), a salt that breaks the frontend join
  (`W008`), an undeclared GDPR owner (`W009`), and required write keys with
  none configured (`W010`).

- **Commands**: `analytics_event_registry`, `analytics_funnel_report`,
  `analytics_fanout`, `purge_analytics`. Retention also runs as
  `stapel_analytics.tasks.purge_analytics_events` (celery optional).

### Not in this release

- The funnel **dashboard** of analytics-standard §3 — this ships the data
  behind it (`/funnels/<slug>/report`, `/reports/events`) and Studio renders
  it (MODULE.md §10).
- The **auto-PR contour** of analytics-standard §5, which depends on
  gateway/contrib_open and is a platform mechanism rather than a module one.
