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
- **the account life cycle** — ``user.merged`` (from stapel-auth). A guest
  absorbed into an existing account takes their funnels with them; what the
  event stream can and cannot do about it is spelled out on the handler.
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


# ─── account life cycle ───────────────────────────────────────────────


@on_action("user.merged")
def handle_user_merged(event):
    """An anonymous guest was absorbed into an existing account.

    Re-points the one per-user column this module owns:

    * :class:`~stapel_analytics.models.Funnel` — ``owner_id``. Plain rewrite;
      the funnel's uniqueness is on ``slug``, deployment-wide, so nothing is
      scoped to the owner and no dedup is needed. Without this the survivor
      gets 403 on a funnel they authored as a guest: the API treats an
      unowned funnel as an operator's object, not a user's.

    **The event stream is deliberately NOT re-keyed, and this is the honest
    reason.** It is not the pseudonymisation: ``privacy.hash_user_id`` is a
    FORWARD hash and ``user.merged`` hands over both raw ids, so
    ``hash_user_id(from_user_id)`` and ``hash_user_id(into_user_id)`` are
    both computable here without reversing anything, salt or no salt. Nothing
    about the hashing stops a merge.

    What stops it is the storage seam. Analytics owns no event table; rows
    live in ``stapel_core.eventstore``, whose ``EventStore`` contract is
    append / query / rollup / purge and has **no update**. A re-key would
    therefore have to be read-all, append-under-the-new-hash, purge-the-old —
    three calls with no transaction spanning them, driven by an at-least-once
    handler. A crash between the append and the purge leaves the guest's
    entire history counted TWICE, under both hashes, in a store whose whole
    job is arithmetic; and a deployment that routed the ``analytics`` stream
    to another backend may not even accept a filtered purge
    (``eventstore.PurgeFiltersUnsupported``). A silent double-count is worse
    than a documented gap, so this handler does not attempt it.

    The gap it leaves, stated plainly rather than left for someone to
    discover: the guest's pre-merge rows keep their own ``user_hash``, so a
    funnel sees them as a second subject, and a later erasure of the survivor
    does not reach them by hash. It reaches some of them by the anon linkage
    (``erasure.linked_anon_ids`` collects the anonymous ids seen beside the
    survivor's hash, and a guest promoted in the same browser shares one) —
    but that is a side effect of same-device promotion, not a guarantee, and
    it must not be read as one. Closing this properly needs an atomic subject
    re-key in ``stapel_core.eventstore``, which is where the primitive
    belongs: every consumer of that seam has the same problem.

    Idempotent: a redelivery finds no funnel under the guest's id and does
    nothing. A malformed or missing id is logged and dropped rather than
    raised — an escaping exception is a poison pill the bus would replay
    forever, and Django's ``UUIDField`` raises ``ValidationError``, which is
    not a ``ValueError``.
    """
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from .models import Funnel

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    try:
        with transaction.atomic():
            moved = Funnel.objects.filter(owner_id=from_user_id).update(
                owner_id=into_user_id
            )
    except (ValidationError, ValueError, TypeError):
        # An id that cannot address a row here names nothing to carry over.
        logger.warning("user.merged with unusable user ids: %s", event.event_id)
        return
    if moved:
        logger.info(
            "user.merged %s -> %s: %s funnel(s) re-owned; the event stream "
            "keeps the guest's own user_hash (no re-key primitive in "
            "stapel_core.eventstore)",
            from_user_id,
            into_user_id,
            moved,
        )


__all__ = [
    "bridged_actions",
    "handle_events_recorded",
    "handle_user_merged",
    "reset_comm_bridge",
    "wire_comm_bridge",
]
