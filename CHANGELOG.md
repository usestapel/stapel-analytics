# Changelog

All notable changes to stapel-analytics are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.4.1] — 2026-09-05

Patch. 0.4.0 shipped a durable outbox, a backoff and a command to run it —
and no scheduled thing that runs the command. This adds the drain.

### The gap

`get_analytics_beat_schedule()` had exactly one entry, the retention purge.
A host that wired it got the conversion outbox with **no sweep**: rows land
`pending`, the backoff sets `next_attempt_at`, and nothing ever comes back
for them unless an operator remembers to type
`manage.py analytics_upload_conversions`. The failure is silent in the worst
way — a pending queue looks exactly like a healthy one, and the rows expire
against Google's 90-day window in the meantime.

**A library that owns a retry owns the thing that runs it.** This module
knows the rows exist, knows when they are due and owns the backoff that
decides it; leaving the last link to each host is the "documented but never
wired" class of defect, and it is not a host's mistake to make.

### Added

- **`stapel_analytics.tasks.upload_click_conversions(limit=100)`** — drains
  `conversions.due(limit)` through `conversions.deliver`, returns counts by
  status, logs them when there is anything to log. Registered as a
  `shared_task` under the stable `UPLOAD_TASK_NAME`
  (`stapel_analytics.tasks.upload_click_conversions`), the same way
  `purge_analytics_events` is, and a plain callable when celery is absent.
- **`analytics-upload-conversions`** — a second entry in
  `get_analytics_beat_schedule()`.
- **`CONVERSION_UPLOAD_SCHEDULE`** (`{"minute": "*/15"}`) — crontab kwargs,
  the same shape and conventions as `PURGE_SCHEDULE`.

### Why quarter-hourly

It is a **quota** decision, not a latency one. Offline conversions are
reported against a 90-day window, so nothing is bought by sweeping every
minute — and something is spent: one conversion is one Google Ads API
operation, and a Basic-access developer token gets 15,000 a day for the
whole deployment. At the task's default limit of 100 rows a pass, `*/15`
tops out near 9,600 uploads a day and leaves the rest of that budget for
everything else the host does with the token. An idle pass costs Google
nothing at all — `due()` is a database query, and no API call happens when
nothing is pending. The cadence also sits above
`GOOGLE_ADS_RETRY_BASE_SECONDS` (300), so the sweep never races a row's own
backoff and re-attempts it early.

### One code path, not two

`manage.py analytics_upload_conversions` now calls
`tasks.upload_click_conversions` instead of reimplementing the loop. Two
copies of "which rows are due and what happens to them" would eventually
disagree about exactly the thing an operator runs the command to check. The
command stays the manual door, its output is unchanged, and **`--dry-run` is
untouched** — it is the one branch that deliberately does not go through the
shared path, because it must write nothing and the task writes. A test
asserts the dry run never reaches the drain.

The task never raises for an empty outbox and never raises for a row Google
refuses: a scheduled task that died on one bad click id would stop
delivering the good rows queued behind it.

Compatible: nothing removed, nothing renamed, no migration.

## [0.4.0] — 2026-09-05

Minor (pre-1.0: minor = breaking, patch = compatible). A new table, a new
comm Function, a new command — and a packaging bug that meant none of this
module's commands were ever in the wheel.

### Offline click conversions go back to Google Ads

Until now this module measured the click and stopped there. The deal the
click led to closes on the phone a week later, in a CRM, at a counter — and
until that outcome is reported back, the ad platform's bidding is
optimizing for form submissions instead of for revenue. Google Ads calls
the fix an offline conversion import: hand back the click identifier it
gave you (`gclid`, or the privacy-preserving `gbraid` / `wbraid` that
replaced it for iOS app↔web journeys) with what the click was eventually
worth.

- **`analytics.upload_click_conversion`** (Function, schema committed) —
  `{click_id, click_id_type, conversion_action, conversion_at, clicked_at?,
  value?, currency?}` in, `{status, reason?}` out.
- **`ConversionUpload`** (`analytics_conversionupload`, migration
  `0002_conversionupload`, additive) — the durable outbox row.
- **`manage.py analytics_upload_conversions`** — `--dry-run`, `--limit`,
  exponential capped backoff honouring `next_attempt_at`.
- **`conversions.py`** — the vendor mapping, and the only place the SDK is
  named.
- **`[google-ads]` extra** — `pip install "stapel-analytics[google-ads]"`.
  Deliberately not in `all`: the tests stub the client at
  `conversions._client_class` precisely so the mapping is proven WITHOUT
  the SDK, and adding a grpc build to every CI matrix leg to test code that
  never calls it is a cost with no verdict attached.

### Why this one is durable when fan-out is not

A failing fan-out adapter is CONTAINED: the event store is the record, the
mirror is rebuildable from it (`analytics_fanout --since`). A conversion
upload has no second copy — the outcome lives in the host's own domain, and
if this module drops it nothing reconstructs it. So the conversion is
written down first and uploaded second, and
`(click_id, conversion_action, conversion_at)` is unique: the same
conversion reported twice, by a retried webhook or a replayed Action or an
operator re-running an importer, is one row and at most one upload.

### Four statuses, because three of them would have required a lie

`uploaded` and `rejected` (Google's message, verbatim) are terminal.
`skipped` means this module refused to send it. **`pending` is the fourth**,
and it is the one the design needs: an upload that could not be ATTEMPTED —
transport, quota, an expired token — is not a verdict about the conversion,
it is a fact about the afternoon. The row is durable and the command retries
it. Reporting `rejected` for a socket error would put a permanent lie in an
outbox row; reporting `skipped` would say a retry is not coming.

### The 90-day window: what is enforced, and what is only approximated

Google refuses a conversion whose CLICK is older than
`GOOGLE_ADS_CONVERSION_WINDOW_DAYS` (90). That distance is click →
conversion, and a conversion event does not carry the click time. So the
input gained an optional **`clicked_at`**, and the module is explicit about
which rule it is actually applying:

- with `clicked_at`: `conversion_at - clicked_at > window` → `skipped` /
  `window`. Google's real rule, enforced locally.
- without it: `now - conversion_at > window`. **Strictly weaker** — a
  conversion older than the window implies a click older than the window,
  so the fallback never skips one Google would have taken, but it does let
  through ones Google rejects.

Those come back `rejected` with Google's own reason rather than silently.
The docstring, MODULE.md §8 and CONFIG.MD all say this rather than
advertising a 90-day guarantee the input cannot support.

### Which skips are terminal, and which are an operator's homework

`window` is terminal (time moves one way). A blank `conversion_action` is
terminal — no configuration change supplies an action the row never
carried. **Missing credentials are not.** The answer is `skipped` /
`not_configured`, but the row stays `pending`: credentials arriving
tomorrow should still upload today's conversions, which are well inside the
window. `analytics.W011` warns at boot when a backlog exists and nothing
can send it.

### Settings

Eleven new keys, all in `STAPEL_ANALYTICS`, all with working defaults or
empty:

`GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`,
`GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`,
`GOOGLE_ADS_LOGIN_CUSTOMER_ID`, `GOOGLE_ADS_CUSTOMER_ID`,
`GOOGLE_ADS_API_VERSION`, `GOOGLE_ADS_CONVERSION_WINDOW_DAYS` (90),
`GOOGLE_ADS_RETRY_BASE_SECONDS` (300), `GOOGLE_ADS_RETRY_MAX_SECONDS`
(86400), `GOOGLE_ADS_MAX_ATTEMPTS` (8).

The credentials are the one group in this module meant to arrive from the
environment, and that is consistent with the house rule rather than an
exception to it: `stapel_core.conf` closes the environment door for keys
that NAME CODE, because a stray `export` must not decide which module the
process loads. A refresh token decides nothing about control flow. It is a
value, it is a secret, and a secret's home is the environment — not a
settings file baked into an image.

### Fixed: this module's management commands were never in the wheel

`[tool.setuptools] packages` listed `stapel_analytics` and
`stapel_analytics.migrations` and nothing else, so `management/` and
`management/commands/` were absent from every wheel this project has
published. `purge_analytics`, `analytics_fanout`,
`analytics_event_registry` and `analytics_funnel_report` have worked for
everyone running from a checkout and for nobody running from an install —
Django says "Unknown command" and nothing else. Both packages are now
declared, and two contract tests pin it: one for the two package names, one
that walks `management/` and asserts every command module lives inside a
declared package.

### Known gap, stated rather than hidden

`ConversionUpload` is **outside the erasure provider**. A click id is an
online identifier, so these rows are personal data by the same argument §7
makes about event rows; they are not erasable today because the row carries
no subject key — it holds a click id and nothing that says whose click it
was. MODULE.md §10 follow-up 7 names the two honest ways out and commits
the next minor to picking one. A deployment that uploads conversions should
meanwhile treat the table as in scope for its own retention policy.

## [0.3.2] — 2026-09-02

Patch. Corrects what 0.3.1 said. The floor itself stays at
`stapel-core>=0.54.1` — for a different, real reason.

**0.3.1's changelog was wrong.** It claimed stapel-core 0.51.0–0.53.0 shipped
wheels missing `stapel_core.django.sites`. The published wheels were checked
afterwards and all of them contain the module; nothing on PyPI was ever
broken. What actually happened is that core's main briefly carried a
`pyproject.toml` whose `[tool.setuptools] packages` list had lost that line to
a rebase conflict resolution — tagged core 0.54.0, caught by core's own CI,
never published, fixed in 0.54.1. Siblings whose CI builds core from git main
rather than PyPI failed at `django.setup()` while it was there.

**The floor is still right, and 0.3.0 stated it wrong.** 0.3.0 declared
`>=0.54.0` because that is the release that added `eventstore.rekey`, which
this module now depends on. 0.54.0 was never published, so the first core on
PyPI carrying the primitive is **0.54.1** — which is what a floor should
name. A floor pointing at a version that does not exist is satisfiable by
accident (pip resolves the next one up) rather than by statement.

## [0.3.1] — 2026-09-02

Patch. Raised `stapel-core` to `>=0.54.1`. **Superseded by 0.3.2 — the floor
is correct, the stated reason was not; see that entry.**

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
