"""comm surface of stapel-analytics.

Three Functions, and the first one is the reason the module has a comm
surface at all: ``analytics.track`` is the server-side track() of
analytics-standard §3 — the way a module in another
container records a step of the same funnel the browser is filling, without
importing this package or knowing where the event store lives.

Every Function carries a JSON schema in ``schemas/functions/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops.
"""
from stapel_core.comm import function


@function("analytics.track")
def track(payload):
    """Record one server-side event.

    Input: ``{"name": str, "props"?: object, "user_id"?, "user_hash"?,
    "anon_id"?, "session_id"?, "source"?}``.
    Output: ``{"accepted": int, "unregistered": bool}``.

    The same registry check and the same PII guard as HTTP ingest — a comm
    caller that could bypass either would be the hole the guard exists to
    close.
    """
    from . import services

    return services.track(
        str(payload["name"]),
        payload.get("props") or {},
        user_id=payload.get("user_id"),
        user_hash=payload.get("user_hash"),
        anon_id=payload.get("anon_id"),
        session_id=payload.get("session_id"),
        source=payload.get("source"),
    )


@function("analytics.event_registry")
def event_registry(payload):
    """The vocabulary this deployment admits.

    Input: ``{}``; output: ``{"events": [...], "mode": str}``. This is what
    a verifier reads to compare a project's declared registry against the
    calls its code actually makes (analytics-standard §4).
    """
    from .registry import event_registry as resolve
    from .registry import registry_mode

    return {
        "events": [
            {
                "name": name,
                "description": str(entry.get("description") or ""),
                "props": dict(entry.get("props") or {}),
                "flow": entry.get("flow") or entry.get("funnel") or None,
            }
            for name, entry in sorted(resolve().items())
        ],
        "mode": registry_mode(),
    }


@function("analytics.funnel_report")
def funnel_report(payload):
    """Conversion by step for one funnel.

    Input: ``{"slug": str, "start"?: iso8601, "end"?: iso8601,
    "compare"?: bool}``; output: the report as a plain dict.

    This is the call Studio makes for a client's "efficiency passport", and
    the one a CTO agent reads to say "you are losing people at the time
    picker" (analytics-standard §3).
    """
    from datetime import datetime

    from .funnels import funnel_report as compute

    def _parse(value):
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    report = compute(
        str(payload["slug"]),
        start=_parse(payload.get("start")),
        end=_parse(payload.get("end")),
        compare=bool(payload.get("compare")),
    )
    return {
        "slug": report.slug,
        "entered": report.entered,
        "completed": report.completed,
        "conversion": report.conversion,
        "window_seconds": report.window_seconds,
        "start": report.start.isoformat() if report.start else None,
        "end": report.end.isoformat() if report.end else None,
        "truncated": report.truncated,
        "steps": [
            {
                "name": step.name,
                "count": step.count,
                "rate_from_first": step.rate_from_first,
                "rate_from_previous": step.rate_from_previous,
                "dropoff": step.dropoff,
                "previous_count": step.previous_count,
                "delta": step.delta,
            }
            for step in report.steps
        ],
    }


@function("analytics.upload_click_conversion")
def upload_click_conversion(payload):
    """Report one offline conversion back to Google Ads.

    Input: ``{"click_id": str, "click_id_type": "gclid"|"gbraid"|"wbraid",
    "conversion_action": str, "conversion_at": iso8601,
    "clicked_at"?: iso8601, "value"?: number, "currency"?: str}``.
    Output: ``{"status": "uploaded"|"rejected"|"skipped"|"pending",
    "reason"?: str}``.

    The call is durable before it is delivered: the conversion is written
    to ``ConversionUpload`` and only then uploaded, so a caller that gets
    an exception has still not lost the conversion. It is idempotent on
    ``(click_id, conversion_action, conversion_at)`` — the same conversion
    reported twice is one row and at most one upload.

    ``pending`` is the fourth status and the honest one: the upload could
    not be ATTEMPTED (transport, quota, an outage), the row is durable, and
    ``manage.py analytics_upload_conversions`` will retry it on the
    configured backoff. Google saying no comes back ``rejected`` with
    Google's own message, which is terminal.

    **On the 90-day window.** Google measures it from the CLICK, and a
    conversion event does not carry the click time — hence the optional
    ``clicked_at``. With it, the real rule is enforced locally and the
    answer is ``skipped`` / ``window``. Without it, this falls back to
    ``now - conversion_at``, which can only be a weaker test: it never
    skips a conversion Google would have taken, but it does let through
    ones Google rejects. ``conversions.py`` documents the trade rather than
    claiming to enforce what it cannot measure.
    """
    from . import conversions

    return conversions.upload_click_conversion(payload)


__all__ = [
    "event_registry",
    "funnel_report",
    "track",
    "upload_click_conversion",
]
