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


__all__ = ["event_registry", "funnel_report", "track"]
