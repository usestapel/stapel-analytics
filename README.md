# stapel-analytics

**The backend half of the Stapel analytics standard.** Your frontend already
declares its events and fires them through `@stapel/analytics` — typed
`track()`, a consent gate, an offline queue, provider fan-out. This is what
it talks to, and what the rest of your fleet talks to.

```
browser (@stapel/analytics)                 server modules
   │ track / page / identify                    │ analytics.track
   ▼                                            ▼
POST /analytics/api/v1/events  ──►  registry check + PII guard
                                            │
   your comm Actions ──► COMM_BRIDGE ────────┤
   (payment.completed, …)                    │
                                             ▼
                                  stapel_core.eventstore  ("analytics" stream)
                                             │
                    ┌────────────────────────┼────────────────────┐
                    ▼                        ▼                    ▼
              funnels / reports      analytics.events.recorded   erasure
              (conversion by step)   ──► adapter fan-out         (GDPR Art. 17)
```

Part of the [Stapel](https://github.com/usestapel) framework.

## Install

```bash
pip install stapel-analytics
```

```python
INSTALLED_APPS = [
    ...,
    "stapel_core.django.eventstore",   # the event rows live here
    "stapel_analytics",
]

path("analytics/", include("stapel_analytics.urls"))   # -> /analytics/api/v1/...
```

That is the whole install. Point your frontend's collector at it and events
start landing — the shipped `stapelCollectorProvider` needs no changes.

## Declare your events once

The registry is the same `analytics/events.json` your frontend's
`gen:events` already produces:

```python
STAPEL_ANALYTICS = {"EVENTS_FILE": BASE_DIR / "analytics" / "events.json"}
```

One vocabulary, two runtimes. An event nobody declared is still stored — and
marked `unregistered`, so you find the typo instead of losing the data.

## Ask where people stop

```python
STAPEL_ANALYTICS = {
    "FUNNELS": {
        "checkout": {
            "title": "Checkout",
            "steps": ["flow.checkout.started", "checkout.address",
                      "payment_completed"],
            "window_seconds": 86400,
        }
    }
}
```

```
GET /analytics/api/v1/funnels/checkout/report?compare=true
```

```json
{"entered": 1204, "completed": 331, "conversion": 0.274917,
 "steps": [
   {"name": "flow.checkout.started", "count": 1204, "rate_from_first": 1.0},
   {"name": "checkout.address",      "count": 502,  "rate_from_previous": 0.416944,
    "dropoff": 702, "delta": -48},
   {"name": "payment_completed",     "count": 331,  "rate_from_previous": 0.659363,
    "dropoff": 171, "delta": 12}
 ]}
```

Notice the last step: `payment_completed` happens on a **server**. One line
of settings makes it a step of the same funnel as the clicks before it:

```python
STAPEL_ANALYTICS = {"COMM_BRIDGE": {"payment.completed": "payment_completed"}}
```

The user id is hashed exactly the way the browser hashes it, so the server
step and the clicks belong to the same person.

## Mirror the stream anywhere

```python
STAPEL_ANALYTICS = {
    "ADAPTERS": {
        "webhook": {"enabled": True, "config": {"url": "https://collect…"}},
        "posthog": {"handler": "app.analytics.posthog", "enabled": True},
    }
}
```

Delivery rides the comm outbox, never the ingest request thread. A vendor's
outage costs you nothing: the event store is the record and
`manage.py analytics_fanout --since …` replays the mirror.

## Close the loop back to the ad platform

Measuring the click is half of it. The deal it led to closes on the phone a
week later, and until that outcome goes back to Google Ads the bidding is
optimizing for form submissions instead of for revenue.

```bash
pip install "stapel-analytics[google-ads]"
```

```python
call("analytics.upload_click_conversion", {
    "click_id": "Cj0KCQ…",             # gclid | gbraid | wbraid
    "conversion_action": "customers/1234567890/conversionActions/42",
    "conversion_at": "2026-09-04T11:02:00Z",
    "clicked_at": "2026-08-30T09:14:00Z",    # optional, and load-bearing
    "value": 4900, "currency": "EUR",
})
# -> {"status": "uploaded"}
```

Durable before it is delivered, and idempotent on
`(click_id, conversion_action, conversion_at)` — the same conversion
reported twice is one row and at most one upload. An upload that could not
be *attempted* comes back `pending`, not `rejected`: the row waits and
`manage.py analytics_upload_conversions` retries it on a capped backoff.
`--dry-run` lists what would go out and writes nothing at all.

`clicked_at` is optional because most callers do not have it, and
load-bearing because Google's 90-day window is measured from the **click**.
With it, that rule is enforced locally. Without it the module falls back to
the conversion's own age — a strictly weaker test, and it says so instead of
advertising a guarantee the input cannot support.

### …or let the platform read the outbox itself

The upload above needs an OAuth client, a refresh token and a developer
token that is granted per account and can be refused. When yours cannot get
one, the same outbox is servable as a file the platform's data manager
fetches on its own schedule — a URL, a token, no credentials of ours:

```bash
curl -u "google:$CONVERSION_FEED_TOKEN" \
     https://example.com/analytics/api/v1/conversions/google-ads.csv
Google Click ID,GBRAID,WBRAID,Conversion Name,Conversion Time,Conversion Value,Conversion Currency
```

In Google Ads Data Manager (HTTPS → Conversions → offline import):

1. **URL** — `https://example.com/analytics/api/v1/conversions/google-ads.csv`
2. **Username** — anything (ignored unless `CONVERSION_FEED_USERNAME` pins it)
3. **Password** — the `CONVERSION_FEED_TOKEN`

Do not put the token in the URL when the fetcher can send a password.
`Authorization: Bearer` and `?token=` remain for fetchers that cannot.

It ships **off** — an empty `CONVERSION_FEED_TOKEN` is a 404, because the
file carries click identifiers and payment values. Serving it never
consumes a row (a re-read answers the same file; the platform deduplicates)
and it writes down that it was read, so
`manage.py analytics_conversion_feed_status` can answer the one question a
pull cannot: has the first load actually landed.

## Privacy is the default, not a setting you remember

- prop values that look like an email or a phone number are **refused**, and
  the receipt names the offending prop;
- user ids are stored as a hash, never raw;
- the erasure provider ships in this same first release — analytics rows are
  personal data, including the anonymous session someone had before they
  logged in:

```python
STAPEL_GDPR = {"DATA_OWNERS": [..., "analytics"]}
```

- and there is a retention horizon out of the box (400 days), with a system
  check if you turn it off.

## Documentation

- `MODULE.md` — the integration contract: wire format, settings, checks,
  comm surface, erasure policy.
- `CONFIG.MD` — every setting, and the four decisions a host actually has to
  make.
- `CHANGELOG.md` — what changed, and what breaks.

## License

MIT
