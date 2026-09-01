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

    Two things carry over, and they are separate mechanisms because they live
    in separate stores:

    * :class:`~stapel_analytics.models.Funnel` — ``owner_id``. Plain rewrite;
      the funnel's uniqueness is on ``slug``, deployment-wide, so nothing is
      scoped to the owner and no dedup is needed. Without this the survivor
      gets 403 on a funnel they authored as a guest: the API treats an
      unowned funnel as an operator's object, not a user's.

    * **The event stream** — every row's ``user_hash``, through
      :func:`store.rekey_subject`. ``privacy.hash_user_id`` is a FORWARD
      hash and ``user.merged`` hands over both raw ids, so both hashes are
      computable here; what used to block this was the storage seam, not the
      pseudonymisation. ``stapel_core.eventstore`` gained ``rekey()`` in
      0.54.0 — atomic, idempotent, and silent — so the guest's history now
      becomes the survivor's in one call instead of the read-append-purge
      sequence that counts it twice when interrupted. That sequence is why
      0.2.0 shipped this gap open on purpose; the primitive is what closed it.

    The two writes are **not** in one transaction, and cannot be: the funnel
    row is in the platform database and the stream may be routed to another
    engine entirely (``STAPEL_EVENTSTORE["ROUTES"]``). They do not need to be.
    Both halves are idempotent, so a redelivery after a partial success
    finishes the job — the funnel update finds nothing left to move, the
    re-key finds no row under the guest's hash, and both return 0.

    Order is deliberate: the stream first. It is the half that can fail for a
    reason outside this deployment's control (a routed backend that predates
    the primitive raises ``RekeyUnsupported``), and failing before the funnel
    move leaves the whole merge visibly unfinished for the redelivery rather
    than half-applied with nothing to show it.

    A backend that cannot re-key is logged as an error and the funnel half
    still runs — the alternative is refusing to re-parent the funnel too,
    which fixes nothing and adds a 403. It is not raised: an escaping
    exception is a poison pill the bus would replay forever, and the
    condition is a deployment's storage choice, not a transient fault.

    Idempotent throughout. A malformed or missing id is logged and dropped
    rather than raised — Django's ``UUIDField`` raises ``ValidationError``,
    which is not a ``ValueError``.
    """
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from . import store
    from .models import Funnel
    from .privacy import hash_user_id

    # Through the seam, never `from stapel_core.eventstore import ...`: the
    # store API lives in exactly one file of this package, and naming the
    # exception here would be the second (tests/test_store.py pins it).
    RekeyUnsupported = store.RekeyUnsupported

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    rekeyed = 0
    try:
        rekeyed = store.rekey_subject(
            from_user_hash=hash_user_id(from_user_id),
            to_user_hash=hash_user_id(into_user_id),
        )
    except RekeyUnsupported:
        # A routed backend that predates the primitive. Say so loudly and
        # keep going: the funnel half is still worth doing, and refusing it
        # would add a 403 to a stream that is already going to be split.
        logger.error(
            "user.merged %s -> %s: the analytics stream is routed to a "
            "backend that cannot re-key, so the guest's rows keep their own "
            "user_hash and a funnel will count them as a second subject "
            "(event %s)",
            from_user_id,
            into_user_id,
            event.event_id,
        )

    try:
        with transaction.atomic():
            moved = Funnel.objects.filter(owner_id=from_user_id).update(
                owner_id=into_user_id
            )
    except (ValidationError, ValueError, TypeError):
        # An id that cannot address a row here names nothing to carry over.
        logger.warning("user.merged with unusable user ids: %s", event.event_id)
        return
    if moved or rekeyed:
        logger.info(
            "user.merged %s -> %s: %s funnel(s) re-owned, %s event row(s) "
            "re-keyed onto the survivor",
            from_user_id,
            into_user_id,
            moved,
            rekeyed,
        )


__all__ = [
    "bridged_actions",
    "handle_events_recorded",
    "handle_user_merged",
    "reset_comm_bridge",
    "wire_comm_bridge",
]
