"""Server-side fan-out — the open adapter registry (analytics-standard §3).

Same merge-registry semantics as everywhere else in the fleet: built-ins <-
``STAPEL_ANALYTICS["ADAPTERS"]`` <- runtime :func:`register_adapter`, last
layer wins, ``None`` removes. An entry merges OVER its built-in rather than
replacing it, and ``config`` merges one level deep, so a host names a URL
without restating a handler it did not write::

    STAPEL_ANALYTICS = {
        "ADAPTERS": {
            "webhook": {"enabled": True, "config": {"url": "https://collect…"}},
            "posthog": {"handler": "app.analytics.posthog", "enabled": True},
        }
    }

**Both built-ins ship disabled.** ``webhook`` cannot be enabled by default
because it has no URL to send to, and ``log`` cannot because writing every
analytics event into the application log is a way to put the data the PII
guard just protected into a log aggregator nobody scoped.

**Delivery is out of band, never inline** (design §3: delivery goes through
the outbox, not inline). Ingest appends to the event store and emits
``analytics.events.recorded`` inside the same transaction; that Action
travels comm's transactional outbox, and the consumer in ``actions.py``
calls :func:`fan_out`. Nothing a third-party adapter does — a slow socket,
a 500, an exception — can be paid for by the browser that sent the batch.

A failing adapter is contained rather than retried: the event store is the
record, fan-out is a mirror, and re-raising into the consumer would make one
broken vendor re-deliver the batch to the three working ones. The recovery
path is explicit and idempotent by time range::

    python manage.py analytics_fanout --since 2026-08-24T00:00:00Z --adapter posthog
"""
from __future__ import annotations

import copy
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: The two universal last miles. A vendor SDK is never a built-in — that is
#: the app layer's file plus one line here, which is exactly the fast-track
#: contribution class analytics-standard §5 is about.
BUILTIN_ADAPTERS: dict[str, Optional[dict]] = {
    "webhook": {
        "handler": "stapel_analytics.adapters.deliver_webhook",
        "enabled": False,
        "config": {"url": "", "headers": {}, "timeout": 10.0},
        "description": "POST the batch as JSON to a collector URL (SSRF-guarded).",
    },
    "log": {
        "handler": "stapel_analytics.adapters.deliver_log",
        "enabled": False,
        "config": {"level": "INFO"},
        "description": "Write each event to the application log (dev mirror of "
                       "the frontend console provider).",
    },
}

#: Runtime overrides, kept apart from the settings layer so tests reset
#: without touching Django settings.
_runtime_adapters: dict[str, Optional[dict]] = {}


class UnknownAdapter(Exception):
    """Raised when an adapter name is not in the effective registry."""


class AdapterError(Exception):
    """An adapter refused or failed to deliver a batch."""


def register_adapter(name: str, spec: Optional[dict]) -> None:
    """Register/override an adapter at runtime. ``None`` removes it."""
    _runtime_adapters[name] = spec


def reset_adapters() -> None:
    """Tests only: drop runtime adapter overrides."""
    _runtime_adapters.clear()


def _merge(base: Optional[dict], overlay: Optional[dict]) -> Optional[dict]:
    if overlay is None:
        return None
    if not isinstance(overlay, dict):
        raise TypeError(
            f"an analytics adapter spec must be an object or None, "
            f"got {type(overlay).__name__}"
        )
    if base is None:
        return dict(overlay)
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if key == "config" and isinstance(value, dict):
            config = dict(merged.get("config") or {})
            config.update(value)
            merged["config"] = config
        else:
            merged[key] = value
    return merged


def get_adapters() -> dict[str, dict]:
    """Effective registry: built-ins <- settings <- runtime, ``None`` removing."""
    from .conf import analytics_settings

    merged: dict[str, Optional[dict]] = copy.deepcopy(BUILTIN_ADAPTERS)
    for source in (analytics_settings.ADAPTERS or {}, _runtime_adapters):
        for name, spec in source.items():
            merged[name] = _merge(merged.get(name), spec)
    return {name: spec for name, spec in merged.items() if spec is not None}


def resolve_adapter(name: str) -> dict:
    """The live spec for *name*, or raise :class:`UnknownAdapter`."""
    try:
        return get_adapters()[name]
    except KeyError:
        raise UnknownAdapter(name) from None


def active_adapters() -> dict[str, dict]:
    """Registered adapters with ``enabled`` truthy — the fan-out targets."""
    return {
        name: spec
        for name, spec in get_adapters().items()
        if spec.get("enabled", True)
    }


def adapter_handler(name: str) -> Callable:
    """Import and return the handler of adapter *name*.

    Resolved at call time, not at registration: a spec is data, and a host
    swapping a handler through settings must not need this module reloaded.
    """
    from django.utils.module_loading import import_string

    spec = resolve_adapter(name)
    handler = spec.get("handler")
    if callable(handler):
        return handler
    if not handler:
        raise UnknownAdapter(f"analytics adapter {name!r} declares no handler")
    return import_string(handler)


def fan_out(events: list, *, only: str | None = None) -> dict:
    """Deliver *events* to every active adapter; return per-adapter counts.

    *events* are payload dicts as stored (``ingest.NormalizedEvent.payload()``
    plus ``ts``), not ORM rows: an adapter must be usable from the outbox
    consumer, from the recovery command, and from a test, and only a plain
    structure is all three.
    """
    targets = active_adapters()
    if only is not None:
        targets = {name: spec for name, spec in targets.items() if name == only}
        if not targets:
            raise UnknownAdapter(only)
    results: dict[str, dict] = {}
    for name, spec in targets.items():
        try:
            handler = adapter_handler(name)
        except Exception as exc:  # unimportable dotted path, no handler
            logger.exception("analytics adapter %r cannot be resolved", name)
            results[name] = {"delivered": 0, "error": str(exc)}
            continue
        try:
            handler(events, dict(spec.get("config") or {}))
        except Exception as exc:
            # Contained on purpose (see the module docstring): the event
            # store already holds the batch, and one vendor's outage must
            # not re-deliver to the adapters that succeeded.
            logger.exception("analytics adapter %r failed on %d event(s)",
                             name, len(events))
            results[name] = {"delivered": 0, "error": str(exc)}
            continue
        results[name] = {"delivered": len(events)}
    return results


# ── Built-in adapters ────────────────────────────────────────────────


def deliver_log(events: list, config: dict) -> None:
    """Log each event. The dev mirror of the frontend's console provider."""
    level = getattr(logging, str(config.get("level") or "INFO").upper(), logging.INFO)
    for event in events:
        logger.log(level, "analytics %s %s %s",
                   event.get("kind"), event.get("name"), event.get("props"))


def deliver_webhook(events: list, config: dict) -> None:
    """POST the batch to a collector URL, through the fleet's SSRF guard.

    The URL comes from configuration rather than from data, so the guard is
    belt-and-braces rather than the only thing standing between a settings
    typo and the cloud metadata endpoint — but a settings typo is exactly
    how that endpoint gets dialled, so the guard runs anyway.
    """
    import json as _json

    from .transport import post_json

    url = str(config.get("url") or "")
    if not url:
        raise AdapterError(
            "the webhook analytics adapter is enabled with no config['url'] "
            "(analytics.W006 says so at boot)"
        )
    headers = {"Content-Type": "application/json"}
    headers.update({str(k): str(v) for k, v in (config.get("headers") or {}).items()})
    body = _json.dumps({"events": events}, default=str).encode("utf-8")
    headers["Content-Length"] = str(len(body))
    post_json(url, body, headers, timeout=float(config.get("timeout") or 10.0))


__all__ = [
    "BUILTIN_ADAPTERS",
    "AdapterError",
    "UnknownAdapter",
    "active_adapters",
    "adapter_handler",
    "deliver_log",
    "deliver_webhook",
    "fan_out",
    "get_adapters",
    "register_adapter",
    "reset_adapters",
    "resolve_adapter",
]
