"""The event registry — the vocabulary this deployment admits.

Semantics are the fleet's merge-registry (stapel-webhooks ``DELIVERY_TYPES``,
stapel-notifications ``TYPES``): built-ins <- ``EVENTS_FILE`` <-
``STAPEL_ANALYTICS["EVENTS"]`` <- runtime :func:`register_event`, last layer
wins, a definition of ``None`` REMOVES an entry — including a built-in one.

A definition is a plain dict in the SAME literal shape ``@stapel/analytics``'
``defineEvent`` projects into ``analytics/events.json``::

    {"name": "listing.published",
     "description": "A seller published a listing",
     "props": {"listing_id": {"type": "string", "description": "…"}},
     "flow": "sell"}

That is not a coincidence and it is the point: the frontend declares events
next to the code that fires them, ``gen:events`` projects the declarations
into ``events.json``, and the same file is the backend's registry. One
vocabulary, two runtimes, no hand-maintained copy in the middle
(analytics-standard §1.1, §4).

Names may be **patterns**: a trailing ``*`` matches a prefix. That is how
``flow.*`` covers the auto-instrumented flow steps (analytics-standard §1.2)
without a release of this module every time a project adds a flow.

Registry validation is a WARNING by default: an unregistered event is stored
and marked, never dropped (§3). Losing the event and the evidence of the
mistake at the same time is the one outcome an analytics ingest must not
have.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Events every deployment has, whatever its project declares.
#:
#: ``flow.*`` is the auto-instrumentation contract: the flow machines of
#: ``@stapel/<module>-react`` emit ``flow.<flow_id>.<step_id>`` on every
#: transition, which is what makes "a funnel is a flow" true without a line
#: of hand-written instrumentation (analytics-standard §1.2). ``identify``
#: is the facade's own event kind — it arrives with ``name: "identify"``
#: and a hashed user id, and a deployment that had to declare it would be
#: declaring the transport.
BUILTIN_EVENTS: dict[str, Optional[dict]] = {
    "flow.*": {
        "name": "flow.*",
        "description": (
            "Auto-instrumented flow step: flow.<flow_id>.<step_id>, emitted by "
            "the frontend flow machines on started/completed/failed transitions."
        ),
        "props": {
            "flow": {"type": "string", "description": "Flow id"},
            "step": {"type": "string", "description": "Step id"},
            "status": {
                "type": "string",
                "description": "started | completed | failed",
                "options": ["started", "completed", "failed"],
            },
        },
    },
    "identify": {
        "name": "identify",
        "description": (
            "The facade's identify() call: a hashed user id, optionally with "
            "non-PII traits. Emitted as kind=identify, never as a track()."
        ),
        "props": {},
    },
}

#: Runtime overrides. Kept apart from the settings layer so a test resets
#: without touching Django settings.
_runtime_events: dict[str, Optional[dict]] = {}

#: Cache of the parsed EVENTS_FILE, keyed by path. Reading a JSON file on
#: every ingest batch would be the module's slowest line.
_file_cache: dict[str, dict] = {}


class UnknownEvent(Exception):
    """Raised when an event name is outside the registry and the mode is strict."""


def register_event(name: str, definition: Optional[dict]) -> None:
    """Register/override an event at runtime.

    ``definition=None`` removes an event a lower layer (built-ins, the
    events file, settings) provided.
    """
    _runtime_events[name] = definition


def reset_events() -> None:
    """Tests only: drop runtime overrides and the events-file cache."""
    _runtime_events.clear()
    _file_cache.clear()


def normalize_definitions(raw) -> dict[str, Optional[dict]]:
    """Accept both shapes a registry is written in and return a name->def map.

    ``events.json`` is a LIST of definitions (each carrying its own
    ``name``); a settings dict is a MAP of name -> definition. Both are
    legal input, because both are what the two producers actually emit, and
    a module that accepted only one of them would make somebody write a
    conversion by hand.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        # A dict whose values are definitions. `None` (a removal) survives.
        out: dict[str, Optional[dict]] = {}
        for name, definition in raw.items():
            if definition is None:
                out[str(name)] = None
            elif isinstance(definition, dict):
                out[str(name)] = {**definition, "name": str(name)}
            else:
                raise TypeError(
                    f"analytics event {name!r} must map to a definition object "
                    f"or None, got {type(definition).__name__}"
                )
        return out
    if isinstance(raw, (list, tuple)):
        out = {}
        for definition in raw:
            if not isinstance(definition, dict) or not definition.get("name"):
                raise TypeError(
                    "an analytics events list must contain objects with a "
                    f"'name', got {definition!r}"
                )
            out[str(definition["name"])] = dict(definition)
        return out
    raise TypeError(
        f"analytics events must be a mapping or a list, got {type(raw).__name__}"
    )


def _events_from_file() -> dict[str, Optional[dict]]:
    """Parse ``EVENTS_FILE`` once. A broken file is loud, not silent.

    A registry the deployment believes it has and does not is exactly the
    failure the whole declare-don't-scatter rule exists to prevent, so a
    missing or malformed file raises ``ImproperlyConfigured`` at first use
    (and ``analytics.E002`` names it at boot, before first use).
    """
    from django.core.exceptions import ImproperlyConfigured

    from .conf import analytics_settings

    path = analytics_settings.EVENTS_FILE
    if not path:
        return {}
    path = str(path)
    cached = _file_cache.get(path)
    if cached is not None:
        return cached
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise ImproperlyConfigured(
            f"STAPEL_ANALYTICS['EVENTS_FILE'] = {path!r} cannot be read: {exc}"
        ) from exc
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"STAPEL_ANALYTICS['EVENTS_FILE'] = {path!r} is not valid JSON: {exc}"
        ) from exc
    # `gen:events` writes {"events": [...]}; a bare list is also accepted.
    if isinstance(raw, dict) and "events" in raw:
        raw = raw["events"]
    parsed = normalize_definitions(raw)
    _file_cache[path] = parsed
    return parsed


def event_registry() -> dict[str, dict]:
    """The effective registry: built-ins <- file <- settings <- runtime.

    Only live (non-``None``) entries are returned, so a removal reads the
    same to every consumer as "was never declared".
    """
    from .conf import analytics_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_EVENTS)
    layers = (
        _events_from_file(),
        normalize_definitions(analytics_settings.EVENTS or {}),
        _runtime_events,
    )
    for layer in layers:
        for name, definition in layer.items():
            merged[name] = definition
    return {
        name: definition
        for name, definition in merged.items()
        if definition is not None
    }


def declared_events() -> dict[str, dict]:
    """Registry entries a PROJECT declared — built-ins excluded.

    ``checks.py`` asks this, not :func:`event_registry`: a deployment whose
    registry is nothing but the built-ins has declared no vocabulary at all,
    and every ``track()`` it receives will be marked unregistered.
    """
    builtins = set(BUILTIN_EVENTS)
    return {
        name: definition
        for name, definition in event_registry().items()
        if name not in builtins
    }


def _matches(pattern: str, name: str) -> bool:
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return pattern == name


def resolve_event(name: str) -> Optional[dict]:
    """The definition matching *name*, or ``None``.

    Exact names win over patterns; among patterns the longest prefix wins,
    so ``flow.checkout.*`` beats ``flow.*`` for a checkout step and the more
    specific declaration is the one a consumer reads.
    """
    registry = event_registry()
    exact = registry.get(name)
    if exact is not None:
        return exact
    best: Optional[dict] = None
    best_length = -1
    for pattern, definition in registry.items():
        if pattern.endswith("*") and _matches(pattern, name):
            if len(pattern) > best_length:
                best, best_length = definition, len(pattern)
    return best


def is_registered(name: str) -> bool:
    """Whether *name* is in the registry (directly or via a pattern)."""
    return resolve_event(name) is not None


def registry_mode() -> str:
    """``"warn"`` | ``"reject"`` | ``"off"`` — what an unknown name does."""
    from .conf import analytics_settings

    mode = str(analytics_settings.REGISTRY_MODE or "warn").lower()
    return mode if mode in ("warn", "reject", "off") else "warn"


def funnel_of(name: str) -> Optional[str]:
    """The flow/funnel an event declares membership of, if any.

    ``flow`` is the key ``defineEvent`` uses; ``funnel`` is accepted as a
    synonym so a registry written against the prose of analytics-standard
    §1.1 ("which funnel it belongs to") is not silently ignored.
    """
    definition = resolve_event(name) or {}
    return definition.get("flow") or definition.get("funnel") or None


__all__ = [
    "BUILTIN_EVENTS",
    "UnknownEvent",
    "declared_events",
    "event_registry",
    "funnel_of",
    "is_registered",
    "normalize_definitions",
    "register_event",
    "registry_mode",
    "reset_events",
    "resolve_event",
]
