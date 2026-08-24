# CONFIG.MD — stapel-analytics settings

Every key below lives in the `STAPEL_ANALYTICS` dict. Resolution order per
key: `settings.STAPEL_ANALYTICS` → flat Django setting of the same name →
environment variable → default. The two seam keys (`SUBJECT_RESOLVER`,
`PII_GUARD`) are **never** read from the environment — a name that decides
which code the process loads is not something a stray `export` may choose.

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
