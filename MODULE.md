# MODULE.md — stapel-analytics

Integration reference for **stapel-analytics**: what it stores, what it
exposes, what it asks of a host, and which of its switches are decisions
rather than tuning. `README.md` is the introduction; this is the contract.

Design of record: `docs/pending/analytics-standard-v2.md` in the stapel
workspace (the backend half; the frontend half is `docs/done/
analytics-standard-v1.md`, shipped as `@stapel/analytics`), plus the
`stapel-analytics` row of `docs/reference/module-roadmap.md`.

---

## 1. What it is

The **backend half of the analytics standard**. The frontend facade shipped
first — `@stapel/analytics` already does `track` / `identify` / `page`, a
consent gate, an offline queue and client-side provider fan-out. What was
missing was everything on the other side of the wire:

```
browser (@stapel/analytics)                 server modules
   │ track / page / identify                    │ analytics.track
   ▼                                            ▼
POST /analytics/api/v1/events  ──►  registry check + PII guard
                                            │
   host comm Actions ──► COMM_BRIDGE ────────┤
   (payment.completed, …)                    │
                                             ▼
                                  stapel_core.eventstore  ("analytics" stream)
                                             │
                    ┌────────────────────────┼────────────────────┐
                    ▼                        ▼                    ▼
              funnels / reports      analytics.events.recorded   erasure
              (conversion by step)   ──► adapter fan-out         (GDPR Art. 17)
```

An **L2 data-plane module** with exactly **two tables**:

| Model | Table | Role |
|---|---|---|
| `Funnel` | `analytics_funnel` | the DEFINITION: ordered event names + a window |
| `ConversionUpload` | `analytics_conversionupload` | the OUTBOX: one offline click conversion on its way to Google Ads (§8) |

The events themselves have **no model here**. They go to
`stapel_core.eventstore` — the fleet's append-only stream primitive, which
is already the design's "partitionable table, retention is a setting". A second unbounded time-series table per deployment, with its
own partitioning and its own scale-out story, would be three problems core
solved once.

App label `analytics`. UUID primary key on `Funnel`, `@access.standard`: a
funnel names business milestones, never personal data. `ConversionUpload`
is `@access.sensitive` and admin-read-only — a click id IS an advertising
identifier of one person, and a hand-edited outbox row is a conversion
uploaded twice or not at all. The EVENT rows are personal data, and they
are administered (and erased) through the event store — see §7.

---

## 2. Mounting

```python
INSTALLED_APPS = [
    ...,
    "stapel_core.django.eventstore",   # REQUIRED: the event rows live here
    "stapel_analytics",
]

# urls.py — the module bakes in the api/v1 segment (api-versioning.md §2)
path("analytics/", include("stapel_analytics.urls"))   # -> /analytics/api/v1/...
```

Forgetting `stapel_core.django.eventstore` is the one mounting mistake that
looks like a working install — the endpoint answers, the registry lists, the
funnels save, and every batch raises on a missing table. `analytics.E001`
refuses to let that boot quietly.

---

## 3. The event registry — the vocabulary this deployment admits

Merge-registry, same semantics as everywhere in the fleet:

```
built-ins  <-  EVENTS_FILE (analytics/events.json)  <-  EVENTS  <-  register_event()
```

Last layer wins; a definition of `None` REMOVES an entry, including a
built-in one. Names may be **patterns**: a trailing `*` matches a prefix, and
an exact name beats a pattern (longest prefix wins among patterns).

A definition is the same literal shape `@stapel/analytics`' `defineEvent`
projects into `analytics/events.json`:

```json
{"name": "listing.published",
 "description": "A seller published a listing",
 "props": {"listing_id": {"type": "string", "description": "…"}},
 "flow": "sell"}
```

That is the point: the frontend declares events next to the code that fires
them, `gen:events` projects them into `events.json`, and the same file is the
backend's registry. **One vocabulary, two runtimes, no hand-maintained copy.**
Both shapes load — a list (what `events.json` contains) and a map (what a
settings dict is), with or without a `{"events": [...]}` envelope.

Built-ins: `flow.*` (the frontend flow auto-instrumentation of
analytics-standard §1.2) and `identify` (the facade's own event kind).

**Validation is a WARNING by default.** An unregistered `track` is stored and
marked `unregistered`, never dropped: losing the event and the evidence of
the mistake at the same time is the one outcome an ingest must not have.
`REGISTRY_MODE = "reject"` refuses instead; `"off"` disables the check (and
says so at boot). Only `kind: "track"` is validated — a `page` name is a path
chosen at render time and an `identify` name is the literal string
`"identify"`.

Three ways to ask:

```python
from stapel_analytics import event_registry
event_registry()["listing.published"]["props"]     # in-process

call("analytics.event_registry", {})               # over comm
GET /analytics/api/v1/event-registry               # over HTTP
python manage.py analytics_event_registry          # from a shell
```

---

## 4. HTTP surface

| Route | Method | Access | Notes |
|---|---|---|---|
| `/events` | POST | **anonymous** | the collector — `@stapel/analytics` posts here |
| `/event-registry` | GET | mandate | the vocabulary, the mode, live adapters, bridged actions |
| `/funnels` | GET, POST | mandate | the caller's funnels + the declared ones (`?mine=`, `?limit=`); POST authors one |
| `/funnels/<slug>` | GET, PATCH, DELETE | owner/staff | PATCH re-validates the WHOLE rule |
| `/funnels/<slug>/report` | GET | owner/staff | `?start=&end=&compare=` |
| `/reports/events` | GET | staff | counts by `name` / `source` / `kind` |
| `/error-keys/` | GET | staff/service | the listing the stapel-translate collector reads |
| `/conversions/google-ads.csv` | GET | **feed token** | the offline-conversion file an ad platform pulls (§8); 404 when no token is configured |

"mandate" is `HasWorkspaceMandateIfScoped` — the library-shaped gate: where a
deployment can answer the mandate question it enforces the third principal
state (a registered account belonging to no workspace is a guest, not a
user), and where nothing can answer it, nobody holds one and it admits. The
strict class would 503 everyone in a single-tenant host, and analytics must
be installable there. Ownership is the scope layered on top.

A stranger's funnel answers **404, not 403**: a slug is guessable, and
"exists but not yours" is an oracle for which funnels another tenant runs —
which is a description of their product roadmap. An *unowned* funnel (created
in code, or by an erased account) answers 403: it is an operator's object.

### Why ingest is open, and what guards it instead

`AllowAny`, and by default an **empty authenticator list**
(`INGEST_AUTHENTICATION`). Three reasons, all of them structural:

1. the identity of an analytics event is the `user_hash` INSIDE the payload,
   already hashed by the browser — a session adds nothing;
2. the last batch of a session arrives via `navigator.sendBeacon`, which
   carries no CSRF token, and DRF's `SessionAuthentication` enforces CSRF
   from inside authentication;
3. the authorization that matters is the SOURCE (`WRITE_KEYS`), not the
   visitor.

What guards it: `MAX_BODY_BYTES`, `MAX_BATCH_SIZE`, `MAX_PROPS_BYTES`,
`MAX_NAME_LENGTH`, `MAX_ID_LENGTH`, the age/skew bounds, the PII guard and —
when a host turns it on — `REQUIRE_WRITE_KEY`. A host that wants
token-authenticated ingest names its classes in `INGEST_AUTHENTICATION`.

### The wire format

```json
POST /analytics/api/v1/events
{
  "write_key": "wk_live_…",          // optional; names the source
  "anon_id": "…", "session_id": "…", // optional batch-level defaults
  "events": [
    {"id": "1755000000000-1",        // the facade's own id
     "kind": "track",                // track | page | identify
     "name": "listing.published",
     "props": {"listing_id": "abc"},
     "userHash": "<sha256 hex>",     // camelCase — what the facade sends
     "ts": 1755000000000}            // epoch MILLISECONDS
  ]
}
```

Every per-event field is read in **both spellings** (`userHash`/`user_hash`,
`anonId`/`anon_id`, `sessionId`/`session_id`): the facade speaks the first,
every server-side producer speaks the second, and making them disagree would
hand somebody a translation layer to write by hand. `traits` is read as
`props` (that is what `identify()` sends). `ts` accepts epoch milliseconds
(what `Date.now()` produces) or an ISO-8601 string.

**Answer: 202 with a receipt**, even when some events were refused.

```json
{"accepted": 19,
 "rejected": [{"index": 7, "name": "checkout.paid", "reason": "pii",
               "detail": "props.contact"}],
 "unregistered": ["checkout.paid"],
 "source": "web"}
```

Partial acceptance is deliberate. The facade retries a batch until its ladder
gives up and DROPS all twenty events, so condemning nineteen good ones for
one bad one loses nineteen. A batch-level fault (bad shape, oversized, missing
write key) is a real 4xx — that one IS the caller's request being wrong.

Rejection reasons: `pii`, `unregistered`, `missing_name`, `name_too_long`,
`unknown_kind`, `not_an_object`, `props_not_an_object`,
`props_not_serializable`, `props_too_large`, `invalid_ts`, `too_old`. Each
maps to an i18n key (`stapel_analytics.errors.REJECTION_KEYS`).

### The legacy alias

`@stapel/analytics` 0.1 hardcodes `COLLECTOR_PATH = "/analytics/api/events"`
— an un-versioned path the fleet canon does not have, and it shipped before
this module existed. So the same view is ALSO mounted there:

```
/analytics/api/v1/events      canon (api-versioning.md §2)
/analytics/api/events         compatibility alias, LEGACY_INGEST_ALIAS
```

It is a compatibility surface with an end date, not a second API — it goes
when the facade targets `api/v1` (§10). `LEGACY_INGEST_ALIAS = False` drops
it today.

---

## 5. Funnels

A funnel is an ordered list of event names plus a conversion window. Two
sources, merged, authored OVER declared:

- **declared** — `STAPEL_ANALYTICS["FUNNELS"]`, the Studio project-spec path
  (analytics-standard §4: the CTO agent declares a funnel together with the
  feature). Read-only over the API: its home is the spec, and an edit the
  next deploy silently reverts is worse than a refusal (409).
- **authored** — rows of the `Funnel` table, created over the API.

**Computation.** One pass over the stream for the step names in the period,
grouped by SUBJECT (`SUBJECT_RESOLVER`: user hash, else anonymous id, else
session id). Per subject, steps are walked in order: the earliest step-1
event opens the window; each later step counts only if it happened at or
after the previous step and **within `window_seconds` of the FIRST one**.
That is the classic conversion window; stating it matters because the
alternative (window from the previous step) gives different numbers for the
same data and neither is "wrong". `window_seconds = 0` means unbounded.

`?compare=true` computes the same funnel over the **equal-length period
immediately preceding** the requested one — the only definition that stays
honest when somebody asks for eleven days.

Bounded by `MAX_REPORT_EVENTS`: a report that hits the bound comes back
`truncated: true` rather than becoming an unbounded scan somebody triggers by
widening a date picker.

Steps are validated **at authoring time** against the registry. A funnel with
a step nothing can emit reports 100% to step 1 and 0% after it forever, and
looks like a product problem rather than a typo.

---

## 6. The comm bridge — server steps of the same funnels

```python
STAPEL_ANALYTICS = {
    "COMM_BRIDGE": {
        # the short form: action name -> analytics event name
        "payment.completed": "payment_completed",
        # the long form
        "email.delivered": {
            "event": "welcome_email_delivered",
            "props": ["template"],                     # payload allowlist
            "mapper": "app.analytics.map_email",       # or a dotted path
        },
    }
}
```

This is what makes a funnel able to END in something that happens on a
server. The subject is taken from the payload the way the fleet names people:
`user_id` (hashed HERE, exactly as the browser hashes it, so the server step
joins the clicks that preceded it), or `user_hash` / `anon_id` / `session_id`
when the emitter already speaks analytics. The raw `user_id` never reaches
the store.

Without an allowlist or a mapper, the bridge carries the payload's **scalar**
keys only. Nested structures are dropped rather than flattened: a bridged
event should read like a milestone, not like a copy of somebody else's
aggregate.

Configured-but-broken is loud: an unimportable or non-callable mapper raises
`ImproperlyConfigured` at `ready()`, because a bridge that quietly does not
fire looks exactly like a funnel with a bad conversion rate.

Server modules can also call it directly:

```python
from stapel_analytics import track
track("payment_completed", {"amount": 10}, user_id=user.id)

call("analytics.track", {"name": "payment_completed", "user_id": str(user.id)})
```

Both go through the same registry check and the same PII guard as HTTP
ingest — an app-layer module that could bypass either would be the hole the
guard exists to close.

---

## 7. Privacy and erasure

**Analytics rows are user data.** A behavioural stream keyed to a person is
personal data whether or not a name appears in it; hashing the user id is
pseudonymisation, not anonymisation. So the erasure provider ships in the
same release as the ingest.

- **PII guard** (`PII_MODE`, default `reject`): prop VALUES that look like an
  email or a phone number refuse the event and the receipt names the prop
  (`props.contact.phone`). `strip` redacts and stores; `warn` logs and
  stores; `off` disables (and `analytics.W002` says so). Keys are never
  judged — `{"email_verified": true}` is not PII and
  `{"note": "call 555-0100"}` is. Same heuristics as the frontend's `pii.ts`,
  with one deliberate one-directional refinement: an ISO-8601 date matches
  the phone SHAPE and is exempted here (the browser still redacts it, so the
  two tiers never disagree about a value that actually travels).
- **User hash**: `sha256(USER_HASH_SALT + user_id)`, and the salt is EMPTY by
  default because `@stapel/analytics`' `hash.ts` hashes unsalted. A salt is a
  real privacy improvement and a real break of the client/server funnel join
  — `analytics.W008` states the trade on every boot.
- **Erasure** (`stapel_analytics.erasure`): owner name `analytics`, subject
  types `account` and `anon`. The policy is **hard delete**, not anonymize —
  an analytics row stripped of its subject cannot be attributed, funnelled or
  reported on, so keeping it is "we kept a little bit".
  `erase_account` also purges the anonymous sessions the person was ever seen
  under, collected BEFORE the first pass because the linking rows are about
  to go. Both protocols reach the same code: the 0.5.0 bus request (wired by
  `stapel_core.gdpr.register_gdpr_owner` in `apps.py` — erasure request,
  owner probe, legacy `user.deleted`) and the in-process
  `AnalyticsGDPRProvider`.
- **Retention** (`RETENTION_DAYS`, default 400 days), applied by
  `manage.py purge_analytics` / `stapel_analytics.tasks.purge_analytics_events`.
  `None` means keep forever, which for personal data is a decision —
  `analytics.W005` accepts either this horizon or the event store's own
  per-stream `RETENTION`.

- **A merge is not a delete** (`actions.handle_user_merged`, `user.merged`
  from stapel-auth). A guest account absorbed into an existing one on
  sign-in is the other half of an account's life cycle, and an app that
  answers only `user.deleted` has a silent wrong answer for it
  (`stapel_core.lifecycle.E001`). This module re-owns `Funnel.owner_id` onto
  the survivor — otherwise the survivor gets 403 on a funnel they authored
  as a guest, because an unowned funnel reads as an operator's object.

  **The event stream is re-keyed too**, through `store.rekey_subject` onto
  `eventstore.rekey` (core **0.54.0**): every row whose `user_hash` is the
  guest's becomes the survivor's, in one atomic, idempotent, silent call.
  `hash_user_id` is a FORWARD hash and the payload carries both raw ids, so
  both hashes are computable in the handler — the pseudonymisation never
  blocked this. What blocked it was the storage seam: the store was append /
  query / rollup / purge with **no update**, so a re-key meant read-all,
  append-under-the-new-hash, purge-the-old — three calls with no transaction
  spanning them, driven by an at-least-once handler, which counts one
  person's history TWICE when interrupted between the append and the purge.
  0.2.0 shipped that gap open rather than take the double count; core grew
  the primitive; 0.3.0 closed it.

  The two writes are **not** in one transaction and cannot be — the funnel is
  in the platform database, the stream may be routed to another engine
  entirely. They do not need to be: both halves are idempotent, so a
  redelivery after a partial success finishes the job and both return 0.
  The stream half runs first, because it is the half that can fail for a
  reason outside this deployment's control.

  A deployment that routed the analytics stream to a backend without `rekey`
  gets `RekeyUnsupported`, which is logged at ERROR and **not** raised (a
  poison pill the bus replays forever, over a storage choice rather than a
  transient fault); the funnel half still runs, and the residue is the old
  gap — the guest's rows keep their own `user_hash`, a funnel counts them as
  a second subject, and a later erasure of the survivor reaches them only by
  the anon linkage (`erasure.linked_anon_ids`), which is a side effect of
  same-device promotion, not a guarantee.
  `tests/test_user_merged.py::TestTheEventStreamFollowsTheSurvivor` and
  `::TestAStoreThatCannotReKey` are the two sides.

Declare the owner in the host:

```python
STAPEL_GDPR = {"DATA_OWNERS": [..., "analytics"]}
```

---

## 8. Server-side fan-out

Open adapter registry, same merge semantics as the event registry:

```
built-ins  <-  ADAPTERS  <-  register_adapter()
```

An entry merges OVER its built-in and `config` merges one level deep, so a
host names a URL without restating a handler it did not write. `None`
removes.

```python
STAPEL_ANALYTICS = {
    "ADAPTERS": {
        "webhook": {"enabled": True, "config": {"url": "https://collect…"}},
        "posthog": {"handler": "app.analytics.posthog", "enabled": True},
    }
}
```

Built-ins: `webhook` (POST the batch as JSON, through the fleet's SSRF guard)
and `log` (dev mirror of the frontend's console provider). **Both ship
disabled** — the webhook has no URL to send to, and writing every analytics
event into the application log puts the data the PII guard just protected
into a log aggregator nobody scoped.

**Delivery is out of band, never inline** (design §3: delivery goes through
the outbox, not inline). Recording appends to the store and emits
`analytics.events.recorded` inside the same transaction; that Action travels
comm's transactional outbox and the consumer in `actions.py` calls the
adapters. Nothing a third-party adapter does can be paid for by the browser
that sent the batch.

A failing adapter is **contained**, not retried: the store is the record,
fan-out is a mirror, and re-raising would re-deliver the batch to the
adapters that succeeded. Recovery is explicit and idempotent by range:

```
python manage.py analytics_fanout --since 2026-08-24T00:00:00Z --adapter posthog
```

A vendor SDK is never a built-in: it is one file in the app layer plus one
line of settings — which is exactly the fast-track contribution class
analytics-standard §5 describes.

### Offline click conversions — the same seam, pointing the other way

Fan-out mirrors *events* to vendors. The conversion uploader sends
*outcomes* back to one: Google Ads offline conversion import. A click is
measured in the browser; the deal it led to closes on the phone a week
later, and until that outcome is reported back the bidding is optimizing
for form submissions instead of for revenue.

```python
call("analytics.upload_click_conversion", {
    "click_id": "Cj0KCQ…",            # gclid | gbraid | wbraid
    "click_id_type": "gclid",
    "conversion_action": "customers/1234567890/conversionActions/42",
    "conversion_at": "2026-09-04T11:02:00Z",
    "clicked_at": "2026-08-30T09:14:00Z",   # optional, and load-bearing — see below
    "value": 4900, "currency": "EUR",
})
# -> {"status": "uploaded" | "rejected" | "skipped" | "pending", "reason"?: str}
```

**Why this one is durable when fan-out is not.** A failing fan-out adapter
is contained because the event store is the record and the mirror can be
rebuilt from it (`analytics_fanout --since`). A conversion upload has no
such second copy: the outcome exists in the host's own domain, and if this
module drops it nothing reconstructs it. So the conversion is written to
`ConversionUpload` first and uploaded second, and
`(click_id, conversion_action, conversion_at)` is unique — the same
conversion reported twice (a retried webhook, a replayed Action, an
operator re-running an importer) is one row and at most one upload.

**Four statuses, and the fourth is the honest one.** `uploaded` and
`rejected` (Google's own message, verbatim) are terminal. `skipped` means
this module refused to send it. `pending` means the upload could not be
**attempted** — transport, quota, an expired token — the row is durable,
and the command retries it on an exponential, capped backoff. "Google said
no" and "we could not ask" must not share a status: the first is a fact
about the conversion, the second is a fact about the afternoon.

```
python manage.py analytics_upload_conversions --dry-run     # lists, writes NOTHING
python manage.py analytics_upload_conversions --limit 500
```

`--dry-run` writes nothing at all — not a status, not a reason, not the
attempt counter. An operator asking "what would go out" must not spend an
attempt from the budget that decides when a row is given up on.

**The drain is scheduled, not left to the host.** A library that owns a
retry owns the thing that runs it: `get_analytics_beat_schedule()` carries
the sweep alongside the retention purge, so a host that wires the schedule
gets the drain with it.

| entry | task | cadence |
|---|---|---|
| `analytics-purge` | `stapel_analytics.tasks.purge_analytics_events` | `PURGE_SCHEDULE` — `{"hour": 4, "minute": 30}` |
| `analytics-upload-conversions` | `stapel_analytics.tasks.upload_click_conversions` | `CONVERSION_UPLOAD_SCHEDULE` — `{"minute": "*/15"}` |

Quarter-hourly is a **quota** decision, not a latency one. Offline
conversions are reported against a 90-day window, so nothing is bought by
sweeping every minute and something is spent: one conversion is one Google
Ads API operation, and a Basic-access developer token gets 15,000 a day for
the whole deployment. At the task's default limit of 100 rows a pass, `*/15`
tops out near 9,600 uploads a day and leaves the rest of the budget for
everything else the host does with that token. An idle pass costs Google
nothing — `due()` is a database query, and no API call happens when nothing
is pending. It also sits above `GOOGLE_ADS_RETRY_BASE_SECONDS` (300), so the
sweep never races a row's own backoff.

The command and the beat entry are **one code path**:
`analytics_upload_conversions` calls `tasks.upload_click_conversions`
rather than reimplementing the loop, because two copies of "which rows are
due and what happens to them" would eventually disagree about exactly the
thing an operator runs the command to check. `--dry-run` is the one branch
that stays out of it — it must write nothing, and the task writes.

**The 90-day window, and what this module can honestly enforce.** Google
refuses a conversion whose CLICK is older than
`GOOGLE_ADS_CONVERSION_WINDOW_DAYS`. That distance is click → conversion,
and a conversion event does not carry the click time — hence the optional
`clicked_at`:

| input | rule enforced locally | what it proves |
|---|---|---|
| `clicked_at` given | `conversion_at - clicked_at > window` | Google's actual rule |
| `clicked_at` absent | `now - conversion_at > window` | strictly weaker: never skips a conversion Google would have taken, but lets through ones it rejects |

The fallback is documented rather than dressed up. A conversion that
passes it and fails at Google comes back `rejected` with Google's reason,
not silently. Supplying `clicked_at` is the difference between a local
refusal and a wasted API call.

**Skips, and which of them are terminal.** `window` is terminal (time only
moves one way). A blank `conversion_action` is terminal — no configuration
change supplies an action the row never carried. Missing **credentials**
are NOT: the answer is `skipped` / `not_configured`, and the row stays
`pending`, because credentials arriving tomorrow should still upload
today's conversions, which are well inside the window. `analytics.W011`
warns when a backlog exists and nothing can send it.

**The SDK is optional.** `google-ads` carries a protobuf runtime; a library
that made every host install it to import `stapel_analytics.models` is a
library nobody mounts. The import happens inside
`conversions._client_class`, at call time — one seam, which is also the one
the tests stub. Install it with the extra:

```
pip install "stapel-analytics[google-ads]"
```

Without it, an upload attempt comes back `pending` with a reason naming the
missing package, and the row waits rather than dying.

---

### The attribution cookie — where the click id comes from

An upload needs a click identifier and, to be judged by the real rule, a
click *time*. Both arrive today through the narrowest door in the funnel:
the frontend reads them off the landing URL and posts them with the
registration. That works only when the visitor lands and signs up in the
same session on the same device. A visitor who lands on the marketing site
on Monday and signs up on Friday brings nothing, and the campaign that paid
for them is credited with nothing.

`AttributionCookieMiddleware` closes that gap by reading a cookie the
marketing site left on the apex domain:

```python
STAPEL_ANALYTICS = {
    "ATTRIBUTION_COOKIE": {
        "NAME": "acme_attr",          # empty (default) = capture is off
        "FORMAT": "base64url_json",
        "FIELDS": {"id": "id", "type": "src", "ts": "ts"},
        "FIRST_TOUCH": True,
        "URL_PARAM": "click_id",
    },
}
MIDDLEWARE = [..., "stapel_analytics.middleware.AttributionCookieMiddleware"]
```

The site writes `NAME=base64url({"id": "<click id>", "src": "gclid", "ts":
<unix seconds>})` on `Domain=.example.com`, `HttpOnly`, `Secure`,
`SameSite=Lax`, ninety days — so every request the browser makes to the
application carries it and no script on any subdomain can read it. That
`HttpOnly` is why the decode is a server job at all.

**What the middleware does, per request.** On any request whose user is
authenticated — an anonymous *account* included, because a guest enrolment
is an account and it can pay — with no attribution stored yet and a
decodable cookie present, it writes one `UserAttribution` row: `click_id`,
`click_id_type`, `clicked_at` (from `ts`), `captured_at` (now),
`source="cookie"`. It runs **after** the view, so it sees whichever of the
fleet's two authentication paths resolved the user, and mount it after the
authentication middleware — the end of `MIDDLEWARE` is the usual answer.
`analytics.W013` fires when the cookie is named and the class is not
mounted.

**First touch wins.** A stored attribution is never overwritten by a
cookie, and a request that carries the explicit `URL_PARAM` stands the
cookie down for that request: the identifier the frontend passes came
through a narrower, more deliberate door. `FIRST_TOUCH: False` inverts it —
the newest click by `clicked_at` wins — and it is a setting because both
answers are defensible.

**Ninety days is checked at capture.** A cookie lives ninety days, so a
click read out of one is routinely at the edge of Google's window. Such a
record is stored with `expired=True` rather than dropped: it is still the
honest answer to "where did this account come from", and the flag is what
keeps a conversion path from enqueueing an upload that can only ever be
settled `expired`. The conversion path reads the row at payment time — a
Stripe webhook carries no cookie — and passes `clicked_at`, which is what
turns the module's weaker fallback rule into the platform's real one.

**Malformed is never an error.** The cookie is somebody else's string. Every
decode failure is dropped and counted
(`analytics.attribution_cookie.malformed`, labelled by reason; captures
count as `analytics.attribution_cookie.captured`), and nothing it can
contain may fail a request.

**Every platform is stored; only Google's are fed.** `CLICK_ID_TYPES`
admits `yclid`, `fbclid` and `ttclid` alongside the three Google takes,
because where an account came from is worth knowing whatever platform sent
it. The conversion feed filters by type, so a `yclid` row never reaches a
file whose columns are Google's. **Follow-up:** a Yandex Direct feed is a
separate file with its own columns and its own upload rules — it is not a
widening of this one.

The row is personal data and leaves the same way everything else here does:
`erasure.erase_account` deletes it and the DSAR export carries it.

### The conversion feed — the same outbox, pulled instead of pushed

The uploader above needs an OAuth client, a refresh token and a developer
token that is granted per account and can be refused. A deployment that
cannot get one has no way to report an offline conversion at all, and the
rows pile up in a table nothing drains. So the same outbox is also servable
as a **file the ad platform fetches itself** — its data manager connects to
an HTTPS source on a schedule it owns, and no credential of ours is
involved.

```
GET /<mount>/api/v1/conversions/google-ads.csv
Authorization: Basic base64(<any username>:<CONVERSION_FEED_TOKEN>)
# or: Authorization: Bearer <CONVERSION_FEED_TOKEN>
# or, for a fetcher that can only be given a URL: ?token=<...>
```

Basic is the door Google's data manager walks through — its HTTPS
connector offers a URL, a username and a password, no bearer and no custom
header — so the token is the **password** and the username is ignored
unless `CONVERSION_FEED_USERNAME` pins it. A fetcher that can send a
password must not be given the token in the URL.

```
Google Click ID,GBRAID,WBRAID,Conversion Name,Conversion Time,Conversion Value,Conversion Currency
Cj0KCQjw…,,,Paid registration (offline import),2026-09-04 11:02:00+03:00,49,EUR
```

The three identifier columns are adjacent and first because the rule they
express is "exactly one of these is filled" — a gbraid written into the
gclid column is not a mistyped value, it is a row the import drops. Column
ORDER is not load-bearing: a data-manager connection maps columns by header
name in its wizard, which is also where these spellings are compared
against the template that account is shown.

**Two windows, and they are different in kind.**

| bound | setting | what it says |
|---|---|---|
| the click window | `GOOGLE_ADS_CONVERSION_WINDOW_DAYS` (90) | the platform refuses a conversion whose click is older, so such a row never reaches the file |
| the feed window | `CONVERSION_FEED_WINDOW_DAYS` (120) | how much history one response carries |

The feed window is deliberately **wider** than the click window. The puller
owns its own schedule and its own retries; a feed that dropped a row the
moment our retention said so would turn one missed fetch into one
permanently lost conversion. So a conversion appears in many consecutive
files and the platform deduplicates on (click id, conversion name,
conversion time).

**Serving does not consume.** The response is a pure function of (now, the
outbox): a re-read answers the same rows, and reading the feed never
changes a row's status. Two things it does do, both bookkeeping:

- it writes a **`ConversionFeedFetch`** receipt — `at`, `rows`, `remote`.
  A push knows it happened; a pull does not, and "has the first load
  landed yet" is the question this endpoint exists to make answerable. Ask
  it with `manage.py analytics_conversion_feed_status`, which prints the
  configuration and the last fetch and never prints the token.
- it settles rows whose click has aged out (below). A pull-only deployment
  runs no drain, so if the feed did not do this the outbox would only grow.

**`expired`, and why it is not `window`.** `window` says the conversion
happened too long after its click — a fact about the pair, true the moment
it was written down. `expired` says the pair was reportable when it arrived
and is not any more, because nobody reported it in time. One is the
caller's data, the other is our latency, and a backlog that could not tell
them apart could not be acted on. Both are terminal — nothing later makes a
click younger — so `conversions.expire_stale()` settles them `skipped` with
a log line naming the row, its click time and the window. It runs on both
doors: at the top of `tasks.upload_click_conversions` (where such a row
would otherwise spend a pass's `--limit` and an attempt from its own
give-up budget every quarter hour) and on every feed fetch.

**It ships off, and off means gone.** `CONVERSION_FEED_TOKEN` is empty by
default, and an empty token is a **404** — the file carries click
identifiers and payment values, and a library that shipped that open would
publish one deployment's conversions to anybody who guessed the path. A
configured feed answers **401** with `WWW-Authenticate: Basic
realm="conversions feed"` to a wrong or missing credential — the challenge a
fetcher may need before it sends the password it holds — compared with
`hmac.compare_digest` because a compare that returns early hands the token
over one character at a time. Every attempt is logged at INFO
(`stapel_analytics.feed`: scheme, status, rows, remote, user-agent — never
the credential), so "the first successful fetch by Google's connector" is a
line a host can grep for. Responses carry `Cache-Control: no-store`.

**Mounting the feed alone.** A service that installs this module only to
hand a platform a file has no collector to expose, and mounting the full
URLconf would put an anonymous ingest route on it as a side effect:

```python
path("billing/analytics/", include("stapel_analytics.urls_feed"))
# -> /billing/analytics/api/v1/conversions/google-ads.csv
```

`urls_feed` re-exports the very same pattern objects `urls_v1` mounts, so
the two doors can never become two different paths. It is also its own
capability gate (`analytics.conversion_feed`).

**The `Conversion Name` column is the whole match.** The import finds the
conversion action by its DISPLAY name in the ad account, character for
character — not by the resource name the API path uses, which stays
`conversion_action` on the row. A mismatch is not an error anywhere: the
fetch succeeds, the file parses, every row is dropped, and the conversion
count simply stays at zero. `analytics.W012` fires when the feed is on and
`CONVERSION_FEED_CONVERSION_NAME` is empty or still the shipped
placeholder.

---

## 9. Settings — `STAPEL_ANALYTICS`

| Key | Default | What it decides |
|---|---|---|
| `EVENTS` | `{}` | merge-registry of event definitions; `None` removes |
| `EVENTS_FILE` | `None` | path to the project's `analytics/events.json` |
| `REGISTRY_MODE` | `"warn"` | `warn` (store+mark) / `reject` / `off` |
| `MAX_BATCH_SIZE` | `500` | events per batch; larger is refused whole |
| `MAX_BODY_BYTES` | `1048576` | request body cap (413) |
| `MAX_PROPS_BYTES` | `16384` | per-event props cap |
| `MAX_NAME_LENGTH` | `200` | longest event/page/source name |
| `WRITE_KEYS` | `{}` | `{write_key: source_name}` |
| `REQUIRE_WRITE_KEY` | `False` | **decision**: refuse an unkeyed batch (401) |
| `DEFAULT_SOURCE` | `"web"` | source when no key names one |
| `INGEST_AUTHENTICATION` | `[]` | authenticator dotted paths for the ingest view only |
| `MAX_CLOCK_SKEW_SECONDS` | `300` | a clock further ahead is corrected to server time |
| `MAX_EVENT_AGE_SECONDS` | `604800` | older events are refused as a stale replay; `0` disables |
| `PII_MODE` | `"reject"` | **decision**: `reject` / `strip` / `warn` / `off` |
| `USER_HASH_SALT` | `""` | **decision**: a salt breaks the frontend funnel join |
| `MAX_ID_LENGTH` | `128` | anon/session id truncation |
| `STREAM` | `"analytics"` | event-store stream name |
| `RETENTION_DAYS` | `400` | **decision**: `None` = keep forever |
| `PURGE_SCHEDULE` | `{"hour": 4, "minute": 30}` | beat cadence for the purge |
| `CONVERSION_UPLOAD_SCHEDULE` | `{"minute": "*/15"}` | beat cadence for the conversion-outbox drain (§8) |
| `QUERY_PAGE_SIZE` | `1000` | rows per store page on a report/erasure pass |
| `MAX_REPORT_EVENTS` | `200000` | scan ceiling; a report past it is `truncated` |
| `FUNNELS` | `{}` | funnels declared by the project spec (read-only over the API) |
| `DEFAULT_FUNNEL_WINDOW_SECONDS` | `604800` | window when a funnel names none |
| `MAX_FUNNEL_STEPS` | `12` | steps per funnel |
| `MAX_FUNNELS_PER_OWNER` | `50` | authored funnels per user |
| `ADAPTERS` | `{}` | merge-registry of fan-out adapters; `None` removes |
| `FANOUT_ENABLED` | `True` | emit `analytics.events.recorded` at all |
| `FANOUT_BATCH_SIZE` | `200` | events per fan-out Action |
| `COMM_BRIDGE` | `{}` | `{action_name: event_name \| spec}` |
| `SERVER_SOURCE` | `"server"` | source on bridged / `track()` events |
| `MAX_PAGE_SIZE` | `100` | cap on the funnel listing (`?limit=` may ask for less) |
| `LEGACY_INGEST_ALIAS` | `True` | mount `/analytics/api/events` for the shipped facade |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | `""` | **secret**, from the environment; empty = uploads answer `not_configured` |
| `GOOGLE_ADS_CLIENT_ID` | `""` | **secret**, OAuth client of the uploading app |
| `GOOGLE_ADS_CLIENT_SECRET` | `""` | **secret** |
| `GOOGLE_ADS_REFRESH_TOKEN` | `""` | **secret**, the offline OAuth grant |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | `""` | manager (MCC) account, only when one is in the path |
| `GOOGLE_ADS_CUSTOMER_ID` | `""` | the advertiser account uploads are written to (dashes stripped) |
| `GOOGLE_ADS_API_VERSION` | `None` | pin the Google Ads API version; `None` = the SDK's default |
| `GOOGLE_ADS_CONVERSION_WINDOW_DAYS` | `90` | Google's click→conversion horizon (§8) |
| `GOOGLE_ADS_RETRY_BASE_SECONDS` | `300` | backoff base for an upload that could not be attempted |
| `GOOGLE_ADS_RETRY_MAX_SECONDS` | `86400` | backoff cap |
| `GOOGLE_ADS_MAX_ATTEMPTS` | `8` | attempts before a row is given up on (`rejected` / `max_attempts`) |
| `ATTRIBUTION_COOKIE` | see §8 | the advertising cookie to capture; `NAME` empty = capture off |
| `CONVERSION_FEED_TOKEN` | `""` | **secret**, the feed's token — Basic password, bearer or `?token=`; empty = the endpoint 404s |
| `CONVERSION_FEED_USERNAME` | `""` | the Basic-auth username the feed insists on; empty = any username |
| `CONVERSION_FEED_WINDOW_DAYS` | `120` | how much history one feed response carries |
| `CONVERSION_FEED_CONVERSION_NAME` | `"Offline conversion"` | the `Conversion Name` column — must match the ad account's display name |
| `SUBJECT_RESOLVER` | `…ingest.default_subject` | dotted path: what "the same person" means |
| `PII_GUARD` | `…privacy.looks_like_pii` | dotted path: the PII heuristic |

`SUBJECT_RESOLVER` and `PII_GUARD` are `import_strings` members: they NAME
CODE, so they are never readable from an environment variable.

### System checks

| Id | Level | Fires when |
|---|---|---|
| `analytics.E001` | Error | the default event store is used and `stapel_core.django.eventstore` is not installed |
| `analytics.E002` | Error | `EVENTS_FILE` cannot be read or parsed |
| `analytics.W001` | Warning | nothing beyond the built-ins is declared |
| `analytics.W002` | Warning | `PII_MODE = "off"` |
| `analytics.W003` | Warning | `REGISTRY_MODE = "off"` |
| `analytics.W004` | Warning | a funnel names steps outside the registry |
| `analytics.W005` | Warning | no retention horizon anywhere |
| `analytics.W006` | Warning | an enabled adapter cannot deliver |
| `analytics.W007` | Warning | the comm bridge targets unregistered events |
| `analytics.W008` | Warning | `USER_HASH_SALT` is set (the join breaks) |
| `analytics.W009` | Warning | `analytics` is not in `STAPEL_GDPR["DATA_OWNERS"]` |
| `analytics.W010` | Warning | `REQUIRE_WRITE_KEY` on with no `WRITE_KEYS` |
| `analytics.W011` | Warning | click conversions are queued and the Google Ads credentials are incomplete |
| `analytics.W012` | Warning | the conversion feed is on and its conversion name is empty or still the placeholder |
| `analytics.W013` | Warning | `ATTRIBUTION_COOKIE['NAME']` is set and `AttributionCookieMiddleware` is not in `MIDDLEWARE` |

---

## 10. comm surface, commands, and the open follow-ups

**Functions** (`schemas/functions/`): `analytics.track`,
`analytics.event_registry`, `analytics.funnel_report`,
`analytics.upload_click_conversion`.
**Emits** (`schemas/emits/`): `analytics.events.recorded`, plus the GDPR
receipts `gdpr.section.erased` / `gdpr.owner.alive`.
**Consumes** (`schemas/consumes/`, documentation only — `autoload_schemas`
registers `emits/` and `functions/`): `gdpr.erasure.requested`,
`gdpr.owner.probe`, `user.deleted`, `user.merged`, plus whatever
`COMM_BRIDGE` names.

**Commands**: `analytics_event_registry`, `analytics_funnel_report`,
`analytics_fanout`, `analytics_upload_conversions`,
`analytics_conversion_feed_status`, `purge_analytics`.

**Follow-ups filed here rather than left implicit:**

1. `@stapel/analytics` should target `/analytics/api/v1/events` and send
   `anon_id` / `session_id`; then `LEGACY_INGEST_ALIAS` can default to
   `False` and be removed a minor later.
2. The frontend's `pii.ts` should adopt the ISO-8601 exemption this module
   ships (§7), so the two guards are identical again rather than
   one-directionally compatible.
3. `post_json` in `transport.py` duplicates the POST shape stapel-webhooks
   also carries around core's GET-only `fetch_bytes`. Both are waiting on a
   `post_bytes` in `stapel_core.net`.
4. The funnel DASHBOARD of analytics-standard §3 (the house dashboard
   pattern, as in stapel-translate) is not built: this ships the data behind it
   (`/funnels/<slug>/report`, `/reports/events`) and Studio renders it.
5. Erasure purges by a JSON payload key, which is correct on every backend
   and slow on a large Postgres stream. The scale-out answer is the event
   store's own (`STAPEL_EVENTSTORE["ROUTES"]` to a column-store backend),
   not a schema here.
6. ~~`stapel_core.eventstore` needs an **atomic subject re-key**~~ —
   **done, core 0.54.0 / analytics 0.3.0.** `eventstore.rekey()` shipped and
   `user.merged` now moves the stream as well as the funnels (§7). The
   primitive went into core, as argued: every library that meters through
   that seam had the same hole. Residual: a deployment that ROUTES the
   analytics stream to a backend without `rekey` still keeps two subject keys
   for one person, logged at ERROR rather than silently.

7. **`ConversionUpload` is outside the erasure provider, and that is a gap
   this release states rather than hides.** A click id is an online
   identifier: rows here are personal data by the same argument §7 makes
   about event rows. They are not erasable today because the row carries
   no subject key — it holds a click id and nothing that says whose click
   it was, deliberately, so it cannot pretend to know. Two honest ways
   out, and the next minor picks one: give the row an optional
   `user_hash` filled by callers that have it (erasable, and one more
   place the hash exists), or give the table a short retention of its own
   — a settled upload has no reason to outlive the window it was uploaded
   against. Until then a deployment that uploads conversions should treat
   the table as in scope for its own retention policy and say so in its
   record of processing.
