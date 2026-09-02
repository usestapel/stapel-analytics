# Changelog

All notable changes to stapel-analytics are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.3.1] — 2026-09-02

Patch. `stapel-core>=0.54.1` — a floor that has to exclude, not just include.

stapel-core 0.51.0 through 0.53.0 shipped wheels missing
`stapel_core.django.sites`: the subpackage was never added to core's explicit
`[tool.setuptools] packages` list, so it was tracked in git, importable from a
checkout, present in an editable install — and absent from the artifact on
PyPI. `stapel_core.django.apps.ready()` imports it unconditionally, so **any**
Django app that resolves one of those three releases dies at
`django.setup()`. The previous floor here admitted all three.

Core 0.54.1 restores the line; this raises the floor past the versions that
cannot work. No code change.

## [0.3.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible). The floor moves to
stapel-core **0.54.0** — the release is a use of a primitive that does not
exist below it.

### The gap 0.2.0 documented is closed

0.2.0 shipped `user.merged` carrying `Funnel.owner_id` and deliberately NOT
re-keying the event stream, with the reasoning written out in three places and
pinned by `TestTheEventStreamIsNotReKeyed`. The reason was never the
pseudonymisation — `hash_user_id` is a forward hash and the payload carries
both raw ids, so both hashes were always computable in the handler. The reason
was the storage seam: `stapel_core.eventstore` was append / query / rollup /
purge with **no update**, so a re-key meant read-all,
append-under-the-new-hash, purge-the-old — three calls with no transaction
across them, run by an at-least-once handler. Interrupted between the append
and the purge, that counts one person's history twice, permanently, in rows
whose only purpose is to be counted.

Core 0.54.0 added `eventstore.rekey()`: atomic, idempotent, silent. So:

### `user.merged` now moves both halves

- `Funnel.owner_id` → the survivor, as before.
- **Every event row's `user_hash`** → the survivor, via the new
  `store.rekey_subject()`. One call, all-or-nothing, and a redelivery moves 0
  the second time because nothing reads the guest's hash any more.

The two writes are **not** in one transaction and cannot be: the funnel lives
in the platform database, the stream may be routed to another engine entirely
(`STAPEL_EVENTSTORE["ROUTES"]`). They do not need to be — both halves are
idempotent, so a redelivery after a partial success finishes the job. The
stream half runs first, deliberately: it is the half that can fail for a
reason outside this deployment's control, and failing before the funnel move
leaves the merge visibly unfinished for the redelivery rather than
half-applied with nothing to show it.

### A routed backend that cannot re-key is loud, not fatal

`RekeyUnsupported` is logged at ERROR, naming both ids and the consequence,
and the funnel half still runs. It is not raised: an escaping exception is a
poison pill the bus replays forever, and the condition is a deployment's
storage choice, not a transient fault. Refusing the funnel move as well would
fix nothing and add a 403. The residue in that deployment is exactly the old
gap, and now it says so at ERROR instead of in a docstring.

### Changed

- `store.rekey_subject(*, from_user_hash, to_user_hash) -> int` — new, and
  the only way this package re-keys. The store API still lives in exactly one
  file: `store.RekeyUnsupported` re-exports the exception type lazily so
  `actions.py` never imports `stapel_core.eventstore` itself
  (`tests/test_store.py::TestSeamIsolation` is what enforces that, and it
  caught the first draft of this change).
- `TestTheEventStreamIsNotReKeyed` → `TestTheEventStreamFollowsTheSurvivor`,
  assertions inverted. `test_no_row_is_duplicated_by_the_merge` is unchanged:
  it was never a statement about the gap, it was the property the gap existed
  to protect, and it still holds now that the gap is shut. New:
  `TestAStoreThatCannotReKey` for the degraded path.
- MODULE.md §10 follow-up 6 closed.

## [0.2.0] — 2026-08-30

### Fixed — a merge is not a delete: the guest's funnels follow the survivor

This module knew half of an account's life cycle. `user.deleted` was
answered from the first release; `user.merged` — stapel-auth folding an
anonymous guest into an existing account when the guest signs in — was not
answered at all, and silence there is not neutrality, it is a wrong answer
given quietly.

`stapel_analytics.actions.handle_user_merged` now re-owns `Funnel.owner_id`
onto the surviving account. Without it the survivor gets **403 on a funnel
they authored as a guest**: `owner_id` keeps pointing at an id that can no
longer sign in, and the API reads an unowned funnel as an operator's object
rather than a user's. Idempotent, and a malformed or missing id is logged and
dropped rather than raised — an escaping exception is a poison pill the bus
would replay forever, and `UUIDField` raises `ValidationError`, which is not
a `ValueError`.

### Not done, and why — the event stream is not re-keyed

The interesting half of this module's merge, written down instead of left to
be discovered.

**The pseudonymisation is not the blocker.** `privacy.hash_user_id` is a
FORWARD hash and `user.merged` carries both raw ids, so
`hash_user_id(from_user_id)` and `hash_user_id(into_user_id)` are both
computable inside the handler, salt or no salt. Nothing about the hashing
stops a merge, and saying otherwise would be an excuse rather than a reason.

**The storage seam is.** Analytics owns no event table; rows live in
`stapel_core.eventstore`, whose contract is append / query / rollup / purge
with **no update**. A re-key would therefore have to be read-all,
append-under-the-new-hash, purge-the-old — three calls with no transaction
spanning them, driven by an at-least-once handler. Interrupted between the
append and the purge it counts one person's history TWICE, in a store whose
whole job is arithmetic; and a deployment that routed the `analytics` stream
elsewhere may refuse a filtered purge outright (`PurgeFiltersUnsupported`). A
silent double-count is worse than a documented gap, so this release does not
attempt it.

**What the gap costs**: the guest's pre-merge rows keep their own
`user_hash`, so a funnel sees them as a second subject, and a later erasure
of the survivor does not reach them *by hash*. It reaches some of them by the
anon linkage — `erasure.linked_anon_ids` collects the anonymous ids seen
beside the survivor's hash, and a guest promoted in the same browser shares
one — but that is a side effect of same-device promotion, not a guarantee,
and it must not be read as one.

Closing it needs an **atomic subject re-key in `stapel_core.eventstore`**,
which is where the primitive belongs: every library metering through that
seam has the same hole. Filed as follow-up 6 in MODULE.md §10, and pinned by
`tests/test_user_merged.py::TestTheEventStreamIsNotReKeyed` so closing it is
a deliberate edit.

### Changed — `stapel-core>=0.52.1`

Core 0.52.1 adds the `stapel_core.lifecycle.E001` system check (tag
`stapel_lifecycle`): an app that subscribes `user.deleted` and not
`user.merged` is a boot-time ERROR. This module's `user.deleted` subscriber
is a closure core registers on its behalf from `register_gdpr_owner`, and
core stamps it with this module's name so the pair is charged here rather
than to core — which is exactly what `tests/test_user_merged.py::
TestSubscription::test_the_lifecycle_pair_check_is_green` asserts. The floor
is raised so that gate can never be skipped for want of the check.

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
