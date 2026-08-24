"""Action subscriptions of stapel-analytics.

Handlers are idempotent-minded (delivery is at-least-once — outbox retries,
broker redelivery). Transport is chosen by ``STAPEL_COMM`` (in-process in a
monolith, bus consumer in microservices); the handler code is identical.

Two consumers live here, and they are different in kind:

- **the fan-out consumer** — ``analytics.events.recorded`` (this module's own
  topic) drives the server-side adapter fan-out. It is a consumer rather than
  a call inside ``record_batch`` for one reason: the browser that sent the
  batch must not pay for a vendor's socket (design §3: delivery goes through
  the outbox, never inline).
- **the comm bridge** — ``STAPEL_ANALYTICS["COMM_BRIDGE"]`` maps a HOST's
  Actions to analytics event names, so a payment, an email, a webhook
  delivery becomes a step of the same funnels as the clicks that led to it
  (analytics-standard §1). The bridge is what makes a funnel able to end in
  something that happens on a server.

The GDPR protocol is NOT hand-written here. ``apps.py`` calls
``stapel_core.gdpr.register_gdpr_owner``, which subscribes the three
handlers every owner library used to copy — ``gdpr.erasure.requested``,
``gdpr.owner.probe`` (answered from the same registration, so
``gdpr.owner.alive`` proves the erasure path is *consumed*), and the legacy
``user.deleted``. What stays this module's own is ``erasure.erase_subject``.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured
from stapel_core.comm import on_action, subscribe_action

from . import events as event_names

logger = logging.getLogger(__name__)


@on_action(event_names.EVENTS_RECORDED)
def handle_events_recorded(event):
    """Fan a recorded batch out to every active adapter.

    Errors are contained inside :func:`adapters.fan_out` per adapter: the
    event store already holds the batch, so re-raising would re-deliver to
    the adapters that succeeded in order to retry the one that did not. The
    recovery path for a real outage is the ``analytics_fanout`` command over
    a time range, which is idempotent in the only way a mirror can be.
    """
    from .adapters import active_adapters, fan_out

    payload = event.payload or {}
    rows = payload.get("events") or []
    if not rows or not active_adapters():
        return
    fan_out(list(rows))


# ─── comm bridge ─────────────────────────────────────────────────────

#: action name -> (event name, mapper|None, prop allowlist|None). Rebuilt
#: atomically by :func:`wire_comm_bridge`; the single dispatcher below reads
#: it at delivery time, so re-wiring (tests, settings overlays) never stacks
#: duplicate subscriptions.
_BRIDGE: dict[str, tuple] = {}


def bridged_actions() -> dict[str, tuple]:
    """The live bridge map — read by ``checks.py`` and the tests."""
    return dict(_BRIDGE)


def _parse_entry(action_name: str, entry):
    """Normalize one ``COMM_BRIDGE`` value into ``(event, mapper, props)``.

    A string is the whole shape most hosts need (``"payment.completed":
    "payment_completed"``). A dict adds a prop allowlist and/or a mapper —
    the mapper being the escape hatch for a payload whose shape analytics
    must never learn (the same seam stapel-docs' INGEST uses).
    """
    from django.utils.module_loading import import_string

    if isinstance(entry, str):
        return entry, None, None
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(
            f"STAPEL_ANALYTICS['COMM_BRIDGE'][{action_name!r}] must be an event "
            f"name or an object, got {type(entry).__name__}"
        )
    event = entry.get("event") or entry.get("name")
    if not event:
        raise ImproperlyConfigured(
            f"STAPEL_ANALYTICS['COMM_BRIDGE'][{action_name!r}] declares no "
            "'event' name"
        )
    mapper = entry.get("mapper")
    if mapper:
        if isinstance(mapper, str):
            try:
                mapper = import_string(mapper)
            except ImportError as exc:
                raise ImproperlyConfigured(
                    f"STAPEL_ANALYTICS['COMM_BRIDGE'][{action_name!r}]['mapper'] "
                    f"= {entry['mapper']!r} cannot be imported"
                ) from exc
        if not callable(mapper):
            raise ImproperlyConfigured(
                f"STAPEL_ANALYTICS['COMM_BRIDGE'][{action_name!r}]['mapper'] "
                "is not callable"
            )
    props = entry.get("props")
    if props is not None and not isinstance(props, (list, tuple)):
        raise ImproperlyConfigured(
            f"STAPEL_ANALYTICS['COMM_BRIDGE'][{action_name!r}]['props'] must be "
            "a list of payload keys"
        )
    return str(event), mapper, (list(props) if props is not None else None)


def wire_comm_bridge() -> None:
    """Resolve ``COMM_BRIDGE`` and subscribe the dispatcher.

    Called from ``apps.py:ready()``; tests re-call it after overriding
    settings. Configured-but-broken must not be silent (system-check failure
    genre): an unimportable or non-callable mapper raises
    :class:`ImproperlyConfigured` instead of a log-and-skip, because a bridge
    that quietly does not fire looks exactly like a funnel with a bad
    conversion rate.

    Reads settings and imports modules only — no database — which is what
    makes it legal at ``ready()`` time (house law §49).
    """
    from .conf import analytics_settings

    resolved: dict[str, tuple] = {}
    for action_name, entry in (analytics_settings.COMM_BRIDGE or {}).items():
        resolved[str(action_name)] = _parse_entry(str(action_name), entry)

    _BRIDGE.clear()
    _BRIDGE.update(resolved)
    for action_name in resolved:
        # subscribe() dedups an identical handler — re-wiring is safe.
        subscribe_action(action_name, _handle_bridged)


def _handle_bridged(event):
    """Record one host Action as an analytics event.

    The subject is taken from the payload the way the fleet names people:
    ``user_id`` (hashed here, exactly as the browser hashes it, so the
    server step joins the client steps of the same person),
    ``user_hash``/``anon_id``/``session_id`` when the emitter already speaks
    analytics.
    """
    from . import services

    entry = _BRIDGE.get(event.event_type)
    if entry is None:
        # Stale subscription: a re-wire dropped this action (there is no
        # unsubscribe in the registry) — inert by design.
        return
    event_name, mapper, allowed = entry
    payload = dict(getattr(event, "payload", None) or {})

    if mapper is not None:
        props = mapper(payload)
        if props is None:
            return
        if not isinstance(props, dict):
            raise ImproperlyConfigured(
                f"comm-bridge mapper for {event.event_type!r} returned "
                f"{type(props).__name__}, expected a dict of props"
            )
    elif allowed is not None:
        props = {key: payload[key] for key in allowed if key in payload}
    else:
        # No allowlist: carry the scalar payload keys. Nested structures are
        # dropped rather than flattened — a bridged event should read like a
        # milestone, not like a copy of somebody else's aggregate.
        props = {
            key: value
            for key, value in payload.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }

    identity = {
        "user_id": payload.get("user_id"),
        "user_hash": payload.get("user_hash"),
        "anon_id": payload.get("anon_id"),
        "session_id": payload.get("session_id"),
    }
    props.pop("user_id", None)
    props.pop("user_hash", None)

    services.track(
        event_name,
        props,
        user_id=identity["user_id"] if not identity["user_hash"] else None,
        user_hash=identity["user_hash"],
        anon_id=identity["anon_id"],
        session_id=identity["session_id"],
    )


def reset_comm_bridge() -> None:
    """Tests only: forget the resolved bridge (subscriptions stay inert)."""
    _BRIDGE.clear()


__all__ = [
    "bridged_actions",
    "handle_events_recorded",
    "reset_comm_bridge",
    "wire_comm_bridge",
]
