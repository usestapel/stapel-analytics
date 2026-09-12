# CONFIG.MD — stapel-analytics settings

Every key below lives in the `STAPEL_ANALYTICS` dict. Resolution order per
key: `settings.STAPEL_ANALYTICS` → flat Django setting of the same name →
environment variable → default. The two seam keys (`SUBJECT_RESOLVER`,
`PII_GUARD`) are **never** read from the environment — a name that decides
which code the process loads is not something a stray `export` may choose.
The `GOOGLE_ADS_*` credentials and `CONVERSION_FEED_TOKEN` are the opposite
case and the environment is exactly where they belong; see those sections
for why the two rules do not contradict each other.

Nothing here needs to be set for the module to boot. What a host actually has
to decide is in §"The five decisions" at the end.

## Event registry

| Key | Default | What it does |
|---|---|---|
| `EVENTS` | `{}` | Merge-registry of event definitions, layered over the built-ins (`flow.*`, `identify`). Accepts a map (`{name: definition}`) or a list (what `events.json` contains). A definition of `None` **removes** an entry, including a built-in. |
| `EVENTS_FILE` | `None` | Path to the project's `analytics/events.json` — the static projection of the frontend's `defineEvent` declarations. Merged UNDER `EVENTS`. Parsed once, lazily; a missing or malformed file is `analytics.E002`, never a silent empty registry. |
| `REGISTRY_MODE` | `"warn"` | `"warn"` stores an unregistered event and marks it; `"reject"` refuses it at ingest; `"off"` disables the check entirely (`analytics.W003`). Only `kind: "track"` is ever validated. |

## Ingest

| Key | Default | What it does |
|---|---|---|
| `MAX_BATCH_SIZE` | `500` | Events one batch may carry. A larger body is refused whole (400). |
| `MAX_BODY_BYTES` | `1048576` | Request body cap (413), checked from `Content-Length` before parsing. |
| `MAX_PROPS_BYTES` | `16384` | Serialized cap on one event's props. |
| `MAX_NAME_LENGTH` | `200` | Longest event / page / source name accepted. |
| `MAX_ID_LENGTH` | `128` | Anonymous / session / hash ids are truncated to this, not refused — an opaque id that is too long is a client bug, not a reason to lose the event. |
| `MAX_CLOCK_SKEW_SECONDS` | `300` | A client clock further ahead than this has its timestamp replaced by server time. Browsers have wrong clocks; refusing those users would bias every funnel toward people with working NTP. |
| `MAX_EVENT_AGE_SECONDS` | `604800` | Older events are refused as a stale offline-buffer replay — admitting them rewrites a closed reporting period. `0` disables the bound. |
| `WRITE_KEYS` | `{}` | `{write_key: source_name}`. What a batch's `write_key` is checked against and what names its `source`. |
| `REQUIRE_WRITE_KEY` | `False` | **Decision.** Refuse a batch whose key is unrecognized (401). Ships off: the shipped facade sends a key only when a host configured one, and a module that 401s a correct frontend out of the box is a module nobody mounts. `analytics.W010` catches "required, none configured". |
| `DEFAULT_SOURCE` | `"web"` | Source attributed to a batch no key names. |
| `INGEST_AUTHENTICATION` | `[]` | Authenticator dotted paths for the ingest endpoint ONLY. Empty on purpose: the ingest identity is the `user_hash` in the payload, and an empty list is what makes `navigator.sendBeacon` work (a beacon carries no CSRF token, and DRF's `SessionAuthentication` enforces CSRF from inside authentication). |
| `LEGACY_INGEST_ALIAS` | `True` | Also mount the un-versioned `/analytics/api/events` that `@stapel/analytics` 0.1 hardcodes. A compatibility surface with an end date — set `False` once your frontend targets `api/v1`. |

## Privacy

| Key | Default | What it does |
|---|---|---|
| `PII_MODE` | `"reject"` | **Decision.** What a PII-shaped prop VALUE does: `"reject"` refuses the event and names the prop; `"strip"` redacts and stores; `"warn"` logs and stores; `"off"` disables the guard (`analytics.W002`). Keys are never judged — only values. |
| `USER_HASH_SALT` | `""` | **Decision.** Mixed into the user id before hashing. Empty by default because `@stapel/analytics` hashes `sha256(userId)` unsalted: with a salt, a server-side funnel step and the browser steps of the same person get different subject keys and never join. `analytics.W008` states the trade on every boot. |
| `PII_GUARD` | `stapel_analytics.privacy.looks_like_pii` | The heuristic itself (dotted path, never from the environment). Same contract as the frontend guard, so tightening one is a choice rather than a coincidence. |

## Storage

| Key | Default | What it does |
|---|---|---|
| `STREAM` | `"analytics"` | The `stapel_core.eventstore` stream every row is appended to. Backend, buffering, partitioning and per-stream routing are the event store's own settings (`STAPEL_EVENTSTORE`) — this module never touches a backend. |
| `RETENTION_DAYS` | `400` | **Decision.** Raw retention applied by `manage.py purge_analytics` / `stapel_analytics.tasks.purge_analytics_events`. `None` = keep forever, which for personal data is a decision somebody made — `analytics.W005` accepts either this or `STAPEL_EVENTSTORE["RETENTION"]`. |
| `PURGE_SCHEDULE` | `{"hour": 4, "minute": 30}` | Crontab kwargs for the beat entry. |
| `CONVERSION_UPLOAD_SCHEDULE` | `{"minute": "*/15"}` | Crontab kwargs for the conversion-outbox drain (`stapel_analytics.tasks.upload_click_conversions`, MODULE.md §8). Quarter-hourly is a quota decision, not a latency one: one conversion is one Google Ads API operation, a Basic-access developer token gets 15,000 a day for the whole deployment, and at the task's default of 100 rows a pass this tops out near 9,600. An idle pass costs Google nothing — no rows due, no API call. It also sits above `GOOGLE_ADS_RETRY_BASE_SECONDS`, so the sweep never races a row's own backoff. |
| `QUERY_PAGE_SIZE` | `1000` | Rows per store page on a report or erasure pass. |
| `MAX_REPORT_EVENTS` | `200000` | Ceiling on the rows one report scans. Past it the report comes back `truncated: true` rather than becoming an unbounded table scan somebody triggers from a dashboard. |

## Funnels

| Key | Default | What it does |
|---|---|---|
| `FUNNELS` | `{}` | Funnels DECLARED by the project spec: `{slug: {"title", "steps", "window_seconds"}}`. Merged UNDER the `Funnel` table — a declared funnel is read-only over the API (409), because its home is the spec and an edit the next deploy reverts is worse than a refusal. |
| `DEFAULT_FUNNEL_WINDOW_SECONDS` | `604800` | Window a funnel that names none inherits. `0` on a funnel means unbounded. |
| `MAX_FUNNEL_STEPS` | `12` | Steps one funnel may carry. |
| `MAX_FUNNELS_PER_OWNER` | `50` | Authored funnels per user. Unowned funnels (created in code) are not capped — they are operators' objects. |
| `SUBJECT_RESOLVER` | `stapel_analytics.ingest.default_subject` | What "the same person" means for a funnel (dotted path, never from the environment). Default: user hash → anonymous id → session id. |

## Server-side fan-out

| Key | Default | What it does |
|---|---|---|
| `ADAPTERS` | `{}` | Merge-registry over the built-ins (`webhook`, `log`), both of which ship DISABLED. An entry merges OVER its built-in and `config` merges one level deep, so a host names a URL without restating a handler. `None` removes an adapter. |
| `FANOUT_ENABLED` | `True` | Whether recording emits `analytics.events.recorded` at all. Off stops the mirror; the event store still records everything. |
| `FANOUT_BATCH_SIZE` | `200` | Events one fan-out Action carries; larger batches are chunked. |

## comm bridge

| Key | Default | What it does |
|---|---|---|
| `COMM_BRIDGE` | `{}` | `{action_name: event_name}` or `{action_name: {"event", "props": [...], "mapper": "dotted.path"}}`. A host Action becomes a step of the same funnels as the clicks that led to it. Empty by default: a bridge that guessed which Actions are business milestones would invent a funnel nobody declared. A broken mapper raises `ImproperlyConfigured` at `ready()` — a bridge that quietly does not fire looks exactly like a bad conversion rate. |
| `SERVER_SOURCE` | `"server"` | Source recorded on bridged and `track()`-ed events. |

## Google Ads offline conversions

The upload of an offline click conversion back to Google Ads
(`conversions.py`, MODULE.md §8). The **pull** half of the same story — a
file the ad platform fetches itself — is the next section. Every key ships empty or with a working
default: a deployment that does not use the uploader configures nothing,
and one that half-configures it gets `skipped` / `not_configured` rather
than a failure deep inside the vendor SDK.

**These are the only keys in this module meant to arrive from the
environment, and that is consistent with the house rule rather than an
exception to it.** The rule (`stapel_core.conf`) closes the environment
door for keys that NAME CODE — `SUBJECT_RESOLVER`, `PII_GUARD` — because a
stray `export` must not decide which module the process loads. A refresh
token decides nothing about control flow: it is a value, it is a secret,
and a secret's home is the environment (or a vault that populates it),
never a settings file baked into an image.

| Key | Default | What it does |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | `""` | **Secret.** The Google Ads API developer token of the account making the calls. |
| `GOOGLE_ADS_CLIENT_ID` | `""` | **Secret.** OAuth client id of the uploading application. |
| `GOOGLE_ADS_CLIENT_SECRET` | `""` | **Secret.** OAuth client secret. |
| `GOOGLE_ADS_REFRESH_TOKEN` | `""` | **Secret.** The offline OAuth grant the uploader runs under. |
| `GOOGLE_ADS_CUSTOMER_ID` | `""` | The advertiser account conversions are written to. Written `123-456-7890` and sent `1234567890`; the dashes are stripped for you. |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | `""` | The manager (MCC) account in the authentication path, when there is one. Deliberately NOT part of the required set: demanding it from a direct advertiser would make a correct configuration look broken. |
| `GOOGLE_ADS_API_VERSION` | `None` | Pin the Google Ads API version (e.g. `"v18"`). `None` uses whatever the installed SDK defaults to. A version is a compatibility decision a deployment makes on its own cadence — not one a patch release of this library should move under it. |
| `GOOGLE_ADS_CONVERSION_WINDOW_DAYS` | `90` | Google's horizon, measured **from the click**. Enforced locally against `clicked_at` when the caller supplies it, and against `conversion_at` otherwise — a strictly weaker test, and `conversions.py` says so rather than claiming to enforce what it cannot measure. |
| `GOOGLE_ADS_RETRY_BASE_SECONDS` | `300` | Backoff base for an upload that could not be ATTEMPTED (transport, quota, an outage). Never applied to a verdict: Google saying no is terminal. |
| `GOOGLE_ADS_RETRY_MAX_SECONDS` | `86400` | Backoff cap. What keeps a long outage bounded instead of exponential forever. |
| `GOOGLE_ADS_MAX_ATTEMPTS` | `8` | Attempts before a row is given up on and marked `rejected` / `max_attempts`. A retry queue with no floor hides a permanently wrong credential behind ever-longer silences. |

The SDK itself is an optional extra — `pip install
"stapel-analytics[google-ads]"`. Without it the library imports and runs
normally and an upload comes back `pending` with a reason naming the
missing package.

## The attribution cookie

Where the click identifier comes from when the frontend cannot supply one
(`attribution.py`, MODULE.md §8). A marketing site on the apex domain writes
an `HttpOnly` cookie on a paid landing; every request the browser later
makes to the application carries it, and
`stapel_analytics.middleware.AttributionCookieMiddleware` — mounted **after**
the authentication middleware — turns it into one `UserAttribution` row per
account.

| Key | Default | What it does |
|---|---|---|
| `ATTRIBUTION_COOKIE` | `{"NAME": "", "FORMAT": "base64url_json", "FIELDS": {"id": "id", "type": "type", "ts": "ts"}, "FIRST_TOUCH": True, "URL_PARAM": "click_id"}` | The cookie to capture, merged one level deep over these defaults (`FIELDS` too). **`NAME` is empty and that disables capture**: the cookie's name, envelope and consent rule belong to the site that writes it, and a guessed name either finds nothing or decodes something it was never told about. `FORMAT` names the envelope (`base64url_json`: base64url — padding optional — of a JSON object). `FIELDS` says where the click id, the platform and the unix-seconds click time live inside it. `FIRST_TOUCH` is a **decision**: `True` never lets a cookie overwrite a stored attribution, `False` lets the newest click by `clicked_at` win. `URL_PARAM` names the query parameter whose presence means the request already carries an explicit attribution, so the cookie stands down for it. |

A click already past `GOOGLE_ADS_CONVERSION_WINDOW_DAYS` at capture is
stored with `expired=True` rather than dropped — it is still the honest
answer to where the account came from, and the flag keeps a conversion path
from enqueueing an upload that can only be settled `expired`. A cookie that
does not decode is dropped and counted
(`analytics.attribution_cookie.malformed`); it can never fail a request.
`analytics.W013` fires when the cookie is named and the middleware is not
mounted.

## The conversion feed

The other door onto the same outbox: instead of this deployment calling the
ad platform, the platform's data manager fetches
`/<mount>/api/v1/conversions/google-ads.csv` on a schedule it owns
(`feed.py`, MODULE.md §8). No OAuth client, no developer token — a URL, a
token and a file. A host that has both doors may open both; the rows are
the same rows and the platform deduplicates.

| Key | Default | What it does |
|---|---|---|
| `CONVERSION_FEED_TOKEN` | `""` | **Secret.** The token the feed demands, compared in constant time — as the password of `Authorization: Basic` (the only form Google's data manager can send: URL, username, password), as a bearer, or as `?token=` for a fetcher that can only be given a URL. **Empty disables the endpoint entirely — 404, not "open".** The file carries click identifiers and payment values, so this is the one switch here that had to ship closed. |
| `CONVERSION_FEED_USERNAME` | `""` | The username the Basic door insists on. Empty = any username works, which is the shipped shape: the connector makes a username mandatory to type and the password is the secret. Set it to pin the pair. |
| `CONVERSION_FEED_WINDOW_DAYS` | `120` | How much history one response carries. Deliberately WIDER than the 90-day click window: the puller owns its own schedule and its own retries, and a feed that dropped a row on our retention would turn one missed fetch into one permanently lost conversion. Rows are still gated by `GOOGLE_ADS_CONVERSION_WINDOW_DAYS`, which is the platform's rule, not ours. |
| `CONVERSION_FEED_CONVERSION_NAME` | `"Offline conversion"` | The `Conversion Name` column. The import matches the conversion action by its DISPLAY name in the ad account, character for character — not by the resource name `conversion_action` carries for the API path. A mismatch is not an error anywhere: the fetch succeeds, the file parses, every row is dropped, and the conversion count stays at zero. `analytics.W012` fires when the feed is on and this is empty or still the shipped placeholder. |

Serving the feed never consumes a row — a re-read answers the same file —
but it does two pieces of bookkeeping: it writes a `ConversionFeedFetch`
receipt (`manage.py analytics_conversion_feed_status` prints the last one,
and never the token), and it settles rows whose click has aged past the
window as `skipped` / `expired`. A pull-only deployment runs no drain, so
without that second half the outbox would only ever grow.

## API

| Key | Default | What it does |
|---|---|---|
| `MAX_PAGE_SIZE` | `100` | Cap on the funnel listing. `?limit=` may ask for less, never for more. |

---

## The five decisions

Everything above has a working default. These five are the ones a host
genuinely has to make, and each of them says so at boot if left unmade:

1. **Where does the vocabulary come from?** `EVENTS_FILE` pointed at your
   project's `analytics/events.json`. Without it every event is stored
   `unregistered` (`analytics.W001`) and no funnel can validate its steps.
2. **How long do you keep behavioural data about people?** `RETENTION_DAYS`,
   plus scheduling the purge. `None` is a legitimate answer — made on
   purpose, not by omission (`analytics.W005`).
3. **Is this module a declared data owner?** `STAPEL_GDPR["DATA_OWNERS"]`
   must list `analytics`, or erasure requests never reach it and the
   behavioural rows survive an account's deletion (`analytics.W009`).
4. **Does ingest need a write key?** `REQUIRE_WRITE_KEY` + `WRITE_KEYS`. Off
   is the right default for a first-party SPA on the same origin; on is the
   right answer the moment you have more than one source, or a mobile app
   whose bundle is public.
5. **Salted or joinable user hashes?** `USER_HASH_SALT`. Empty keeps the
   client/server funnel join working; setting it makes the store unlinkable
   to an id guesser and breaks every mixed funnel (`analytics.W008`).
