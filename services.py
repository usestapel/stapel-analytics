"""The domain of stapel-analytics: record, announce, author, report.

Reading order:

1. :func:`record_batch` — one ingest body in, N event-store rows out, plus
   ONE announcement (``analytics.events.recorded``) that drives server-side
   fan-out. This is the whole ingest path; the HTTP view and a comm caller
   go through the same door.
2. :func:`track` — the server-side ``analytics.track`` of §3: an app-layer
   module or the comm bridge records a step of the same funnels the browser
   is filling, with the same registry and the same PII guard.
3. Funnel authoring (:func:`save_funnel`, :func:`delete_funnel`) — the
   validation that decides what a funnel may say, in exactly one place.

The invariant the split exists to protect: **recording is cheap and
transactional, fan-out is slow and external.** Recording appends to the
event store and emits inside the same transaction, so a row and the fact
that it exists are one decision (outbox discipline). Delivering to a vendor
happens in whatever process consumes the Action — never on the request
thread of the browser that sent the batch.
"""
from __future__ import annotations

import logging

from django.db import transaction

from . import events as event_names
from .errors import (
    ERR_400_FUNNEL_SLUG_TAKEN,
    ERR_400_FUNNEL_STEP_UNKNOWN,
    ERR_400_FUNNEL_STEPS,
    ERR_409_FUNNEL_CAP,
)
from .ingest import IngestRefused, NormalizedEvent, normalize_batch, resolve_source
from .privacy import PiiRefused, guard_props, hash_user_id
from .registry import is_registered, registry_mode

logger = logging.getLogger(__name__)


class AnalyticsError(Exception):
    """A refusal with an HTTP status and an i18n error key."""

    def __init__(self, status: int, error_key: str, params: dict | None = None):
        super().__init__(error_key)
        self.status = status
        self.error_key = error_key
        self.params = params or {}


# ── Ingest ───────────────────────────────────────────────────────────


def check_write_key(body: dict) -> str:
    """Resolve the batch's source, refusing an unrecognized key when required.

    ``REQUIRE_WRITE_KEY`` ships OFF (see ``conf.py``): the shipped
    ``@stapel/analytics`` facade sends ``write_key`` only when a host
    configured one, and a module that 401s a correct frontend out of the box
    is a module nobody mounts.
    """
    from .conf import analytics_settings

    write_key = body.get("write_key") or body.get("writeKey")
    source, recognized = resolve_source(write_key)
    if analytics_settings.REQUIRE_WRITE_KEY and not recognized:
        raise IngestRefused("write_key", status=401)
    return source


def record_batch(body: dict) -> dict:
    """Record one ingest body. Returns the receipt the caller answers with.

    ``{"accepted": int, "rejected": [{index, name, reason, detail}],
    "unregistered": [name, ...], "source": str}``.

    Partial acceptance is deliberate (see ``ingest.normalize_batch``): the
    facade retries a batch until it gives up and DROPS all of it, so
    condemning twenty good events for one bad one loses nineteen.
    """
    source = check_write_key(body)
    normalized, rejections = normalize_batch(body, default_source=source)
    accepted = _persist(normalized)
    return {
        "accepted": accepted,
        "rejected": [rejection.as_dict() for rejection in rejections],
        "unregistered": sorted({e.name for e in normalized if e.unregistered}),
        "source": source,
    }


def _persist(normalized: list) -> int:
    """Append to the store and announce, atomically.

    The emit sits INSIDE the transaction on purpose (outbox discipline): the
    announcement leaves iff the rows committed, so fan-out can never mirror
    a batch the store does not have — nor stay silent about one it does.
    """
    from .store import append

    if not normalized:
        return 0
    with transaction.atomic():
        count = append(normalized)
        _announce(normalized)
    return count


def _announce(normalized: list) -> None:
    """Emit ``analytics.events.recorded`` in chunks, for the fan-out consumer."""
    from stapel_core.comm import emit

    from .conf import analytics_settings

    if not analytics_settings.FANOUT_ENABLED:
        return
    chunk_size = max(int(analytics_settings.FANOUT_BATCH_SIZE or 200), 1)
    for start in range(0, len(normalized), chunk_size):
        chunk = normalized[start:start + chunk_size]
        emit(
            event_names.EVENTS_RECORDED,
            {
                "count": len(chunk),
                "events": [
                    {**event.payload(), "ts": event.ts.isoformat()} for event in chunk
                ],
            },
        )


def track(
    name: str,
    props: dict | None = None,
    *,
    user_id=None,
    user_hash: str | None = None,
    anon_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    kind: str = "track",
    ts=None,
) -> dict:
    """Record ONE server-side event — ``analytics.track`` for server modules.

    ``user_id`` is hashed here (never stored raw): a server step must land
    on the same funnel subject as the browser steps of the same person, and
    the browser sends ``sha256(userId)``. Pass ``user_hash`` directly when
    the caller already holds one.

    Same registry and same PII guard as HTTP ingest — an app-layer module
    that could bypass either would be the hole the guard exists to close.
    """
    from django.utils import timezone

    from .conf import analytics_settings

    if user_hash is None and user_id is not None:
        user_hash = hash_user_id(user_id)

    guarded = guard_props(dict(props or {}), event_name=name)

    unregistered = False
    if kind == "track" and registry_mode() != "off" and not is_registered(name):
        if registry_mode() == "reject":
            raise AnalyticsError(400, "error.400.analytics_event_unregistered",
                                 {"event": name})
        unregistered = True
        logger.warning(
            "analytics: server event %r is not in the registry — "
            "stored and marked unregistered", name,
        )

    event = NormalizedEvent(
        name=name,
        kind=kind,
        ts=ts or timezone.now(),
        props=guarded,
        anon_id=anon_id,
        user_hash=user_hash,
        session_id=session_id,
        source=str(source or analytics_settings.SERVER_SOURCE or "server"),
        unregistered=unregistered,
    )
    _persist([event])
    return {"accepted": 1, "unregistered": unregistered}


# ── Funnel authoring ─────────────────────────────────────────────────


def validate_steps(steps) -> list:
    """Refuse a funnel whose steps cannot ever be reached.

    Checked at AUTHORING time, not at report time. The alternative — a
    funnel that quietly reports 0% forever — is discovered by whoever was
    relying on the number, weeks later, and looks like a product problem
    rather than a typo.
    """
    from .conf import analytics_settings

    max_steps = int(analytics_settings.MAX_FUNNEL_STEPS or 12)
    if not isinstance(steps, (list, tuple)):
        raise AnalyticsError(400, ERR_400_FUNNEL_STEPS, {"max": max_steps})
    cleaned = [str(step).strip() for step in steps if str(step).strip()]
    if not 2 <= len(cleaned) <= max_steps:
        raise AnalyticsError(400, ERR_400_FUNNEL_STEPS, {"max": max_steps})
    if registry_mode() != "off":
        for step in cleaned:
            if not is_registered(step):
                raise AnalyticsError(
                    400, ERR_400_FUNNEL_STEP_UNKNOWN, {"step": step}
                )
    return cleaned


def save_funnel(
    *,
    slug: str,
    steps,
    title: str = "",
    description: str = "",
    window_seconds: int | None = None,
    is_active: bool = True,
    owner_id=None,
    workspace_id=None,
    instance=None,
):
    """Create or update a funnel row after validating the whole rule.

    A PATCH re-validates everything, not just what changed: a funnel is a
    small object and half-validating it is how a step name that was legal
    last month survives a registry change nobody re-checked.
    """
    from .conf import analytics_settings
    from .funnels import declared_funnels
    from .models import Funnel

    cleaned = validate_steps(steps)
    window = int(window_seconds or analytics_settings.DEFAULT_FUNNEL_WINDOW_SECONDS or 0)

    if instance is None:
        if Funnel.objects.filter(slug=slug).exists():
            raise AnalyticsError(400, ERR_400_FUNNEL_SLUG_TAKEN, {"slug": slug})
        if slug in declared_funnels():
            # Authoring over a declared slug would make `/funnels/<slug>`
            # mean two different things depending on deploy order.
            raise AnalyticsError(400, ERR_400_FUNNEL_SLUG_TAKEN, {"slug": slug})
        cap = int(analytics_settings.MAX_FUNNELS_PER_OWNER or 0)
        if cap and owner_id is not None:
            if Funnel.objects.filter(owner_id=owner_id).count() >= cap:
                raise AnalyticsError(409, ERR_409_FUNNEL_CAP, {"max": cap})
        instance = Funnel(slug=slug, owner_id=owner_id, workspace_id=workspace_id)

    instance.steps = cleaned
    instance.title = title or instance.title
    instance.description = description or instance.description
    instance.window_seconds = window or instance.window_seconds
    instance.is_active = is_active
    instance.save()
    return instance


def delete_funnel(instance) -> None:
    """Delete an authored funnel. Declared ones are refused by the view."""
    instance.delete()


# ── Retention ────────────────────────────────────────────────────────


def purge_events(*, older_than=None) -> int:
    """Drop analytics rows past the retention horizon; return how many.

    ``RETENTION_DAYS = None`` means "keep forever", which for rows that are
    personal data is a decision rather than a default — so it is a no-op
    here and a warning at boot (``analytics.W005``), not a silent skip.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .conf import analytics_settings
    from .store import purge

    if older_than is None:
        days = analytics_settings.RETENTION_DAYS
        if not days:
            return 0
        older_than = timezone.now() - timedelta(days=int(days))
    return purge(older_than=older_than)


__all__ = [
    "AnalyticsError",
    "IngestRefused",
    "PiiRefused",
    "check_write_key",
    "delete_funnel",
    "purge_events",
    "record_batch",
    "save_funnel",
    "track",
    "validate_steps",
]
