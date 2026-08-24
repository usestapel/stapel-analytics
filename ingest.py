"""Wire format: what ``@stapel/analytics`` sends, and what gets stored.

This module is the compatibility contract with the frontend facade that
shipped first (analytics-standard v1, ``@stapel/analytics``
``providers.ts``). The facade POSTs::

    POST /analytics/api/v1/events        (and the /analytics/api/events alias)
    {
      "write_key": "…",                        # only when the host set one
      "events": [
        {"id": "1723..-1", "kind": "track",
         "name": "listing.published",
         "props": {...},
         "userHash": "<sha256 hex>",           # only after identify()
         "ts": 1723000000000}                  # epoch MILLISECONDS
      ]
    }

Three fields the design's storage row wants (``anon_id``, ``session_id``,
``source``) are NOT in that payload — the shipped facade has no notion of
them. They are therefore accepted, never required, at two levels: on the
batch (applying to every event) and on the event (overriding the batch).
Both ``camelCase`` and ``snake_case`` spellings are read, because the
facade speaks the first and every server-side producer speaks the second,
and a module that made the two disagree would be handing somebody a
translation layer to write by hand.

Normalization refuses rather than guesses. A batch that is too large, an
event with no name, a timestamp from next year, a prop tree that looks like
a phone number — each is refused BY NAME, per event where the fault is per
event, and the response says how many were accepted and why the rest were
not. Silence about a dropped event is the one thing an ingest may not do:
the whole point of the layer is to be the record.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .privacy import PiiRefused, guard_props
from .registry import is_registered, registry_mode

logger = logging.getLogger(__name__)

#: The three event kinds the facade produces. ``track`` is the only one
#: validated against the event registry: a ``page`` event's ``name`` is a
#: page path chosen at render time and an ``identify`` event's name is the
#: literal string "identify" (see ``createAnalytics.ts``), so registering
#: either would mean registering the transport instead of the vocabulary.
KINDS = ("track", "page", "identify")
REGISTERED_KINDS = ("track",)


class IngestRefused(Exception):
    """The whole batch is refused (size, encoding, write key).

    Distinguished from a per-event rejection on purpose: a batch-level fault
    is the caller's request being wrong, and answering 202 with "0 accepted"
    would let a misconfigured collector retry forever against a 2xx.
    """

    def __init__(self, error_key: str, params: dict | None = None, status: int = 400):
        super().__init__(error_key)
        self.error_key = error_key
        self.params = params or {}
        self.status = status


@dataclass
class NormalizedEvent:
    """One event on its way into the store."""

    name: str
    kind: str
    ts: datetime
    props: dict = field(default_factory=dict)
    anon_id: str | None = None
    user_hash: str | None = None
    session_id: str | None = None
    source: str = "web"
    event_id: str | None = None
    unregistered: bool = False

    def payload(self) -> dict:
        """The event-store payload — the row as it is queried and erased.

        ``unregistered`` is written only when true. A boolean that is false
        on 99.9% of rows is bytes in every row of a table that grows without
        bound, and its absence reads identically.
        """
        row = {
            "name": self.name,
            "kind": self.kind,
            "props": self.props,
            "source": self.source,
        }
        for key, value in (
            ("anon_id", self.anon_id),
            ("user_hash", self.user_hash),
            ("session_id", self.session_id),
            ("event_id", self.event_id),
        ):
            if value:
                row[key] = value
        if self.unregistered:
            row["unregistered"] = True
        return row


@dataclass
class Rejection:
    """One refused event and why — echoed back so a client can fix it."""

    index: int
    name: str
    reason: str
    detail: str = ""

    def as_dict(self) -> dict:
        row = {"index": self.index, "name": self.name, "reason": self.reason}
        if self.detail:
            row["detail"] = self.detail
        return row


def default_subject(row: dict) -> str | None:
    """The funnel subject of a stored row: user hash, else anon, else session.

    The order is the identity ladder, strongest first. It is the
    ``SUBJECT_RESOLVER`` seam's default because "the same person" is exactly
    the definition a deployment with its own identity model has to be able
    to change without forking the funnel engine.
    """
    return row.get("user_hash") or row.get("anon_id") or row.get("session_id") or None


def _pick(source: dict, *names):
    """First present, non-empty value among *names* — camelCase or snake."""
    for name in names:
        value = source.get(name)
        if value not in (None, ""):
            return value
    return None


def _clean_id(value, limit: int) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:limit] if len(text) > limit else text


def _parse_ts(raw, now: datetime, skew: int, max_age: int):
    """``(ts, reason|None)`` for one client timestamp.

    Milliseconds since the epoch (what ``Date.now()`` produces) is the
    contract; an ISO-8601 string is accepted too, because a server-side
    producer writing JSON by hand will reach for one. A clock ahead of the
    server is CORRECTED rather than refused — browser clocks are wrong all
    the time and dropping those users' events would silently bias every
    funnel toward people with working NTP. A timestamp older than
    ``MAX_EVENT_AGE_SECONDS`` IS refused: at that age it is somebody
    replaying a stale offline buffer, and admitting it rewrites a closed
    reporting period.
    """
    if raw in (None, ""):
        return now, None
    parsed = None
    if isinstance(raw, bool):
        parsed = None
    elif isinstance(raw, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            parsed = None
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed is None:
        return now, "invalid_ts"
    if parsed > now + timedelta(seconds=skew):
        # A clock ahead of ours: keep the event, use server time.
        return now, None
    if max_age and parsed < now - timedelta(seconds=max_age):
        return parsed, "too_old"
    return parsed, None


def resolve_source(write_key, fallback: str | None = None) -> tuple[str, bool]:
    """``(source_name, key_was_recognized)`` for a batch's write key."""
    from .conf import analytics_settings

    keys = analytics_settings.WRITE_KEYS or {}
    if write_key and str(write_key) in keys:
        return str(keys[str(write_key)]), True
    return str(fallback or analytics_settings.DEFAULT_SOURCE or "web"), False


def normalize_batch(body: dict, *, default_source: str | None = None):
    """``(events, rejections)`` for one ingest body.

    Raises :class:`IngestRefused` for a fault of the batch itself; a fault
    of one event becomes a :class:`Rejection` and the rest of the batch is
    still accepted. Partial acceptance is deliberate — an offline buffer
    that accumulated one bad event must not be condemned whole, because the
    facade will simply retry the same batch until it gives up and drops all
    twenty of them.
    """
    from django.utils import timezone as django_timezone

    from .conf import analytics_settings

    if not isinstance(body, dict):
        raise IngestRefused("batch_not_object")

    raw_events = body.get("events")
    if raw_events is None:
        raise IngestRefused("batch_missing_events")
    if not isinstance(raw_events, list):
        raise IngestRefused("batch_events_not_list")

    max_batch = int(analytics_settings.MAX_BATCH_SIZE or 500)
    if len(raw_events) > max_batch:
        raise IngestRefused("batch_too_large", {"max": max_batch, "got": len(raw_events)})

    max_name = int(analytics_settings.MAX_NAME_LENGTH or 200)
    max_id = int(analytics_settings.MAX_ID_LENGTH or 128)
    max_props = int(analytics_settings.MAX_PROPS_BYTES or 16384)
    skew = int(analytics_settings.MAX_CLOCK_SKEW_SECONDS or 0)
    max_age = int(analytics_settings.MAX_EVENT_AGE_SECONDS or 0)
    mode = registry_mode()
    now = django_timezone.now()

    batch_source = default_source or str(analytics_settings.DEFAULT_SOURCE or "web")
    batch_anon = _clean_id(_pick(body, "anon_id", "anonId"), max_id)
    batch_session = _clean_id(_pick(body, "session_id", "sessionId"), max_id)

    events: list[NormalizedEvent] = []
    rejections: list[Rejection] = []

    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            rejections.append(Rejection(index, "", "not_an_object"))
            continue

        name = _pick(raw, "name", "event")
        kind = str(_pick(raw, "kind") or "track").lower()
        if kind not in KINDS:
            rejections.append(Rejection(index, str(name or ""), "unknown_kind", kind))
            continue
        if not name:
            # identify() carries no caller-chosen name; the facade sends the
            # literal "identify" and a hand-rolled producer forgets to.
            if kind == "identify":
                name = "identify"
            else:
                rejections.append(Rejection(index, "", "missing_name"))
                continue
        name = str(name)
        if len(name) > max_name:
            rejections.append(Rejection(index, name[:max_name], "name_too_long"))
            continue

        props = raw.get("props")
        if props is None:
            props = raw.get("traits") or {}
        if not isinstance(props, dict):
            rejections.append(Rejection(index, name, "props_not_an_object"))
            continue
        try:
            encoded = len(json.dumps(props, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            rejections.append(Rejection(index, name, "props_not_serializable"))
            continue
        if encoded > max_props:
            rejections.append(
                Rejection(index, name, "props_too_large", f"{encoded} > {max_props}")
            )
            continue

        try:
            props = guard_props(props, event_name=name)
        except PiiRefused as exc:
            rejections.append(Rejection(index, name, "pii", exc.path))
            continue

        ts, ts_problem = _parse_ts(_pick(raw, "ts", "timestamp"), now, skew, max_age)
        if ts_problem == "too_old":
            rejections.append(Rejection(index, name, "too_old", ts.isoformat()))
            continue
        if ts_problem == "invalid_ts":
            rejections.append(Rejection(index, name, "invalid_ts"))
            continue

        unregistered = False
        if kind in REGISTERED_KINDS and mode != "off" and not is_registered(name):
            if mode == "reject":
                rejections.append(Rejection(index, name, "unregistered"))
                continue
            unregistered = True
            logger.warning(
                "analytics: event %r is not in the registry "
                "(analytics-standard §1.1) — stored and marked unregistered", name,
            )

        events.append(
            NormalizedEvent(
                name=name,
                kind=kind,
                ts=ts,
                props=props,
                anon_id=_clean_id(_pick(raw, "anon_id", "anonId"), max_id) or batch_anon,
                user_hash=_clean_id(_pick(raw, "user_hash", "userHash"), max_id),
                session_id=(
                    _clean_id(_pick(raw, "session_id", "sessionId"), max_id)
                    or batch_session
                ),
                source=str(_pick(raw, "source") or batch_source)[:max_name],
                event_id=_clean_id(_pick(raw, "event_id", "id"), max_id),
                unregistered=unregistered,
            )
        )

    return events, rejections


__all__ = [
    "KINDS",
    "REGISTERED_KINDS",
    "IngestRefused",
    "NormalizedEvent",
    "Rejection",
    "default_subject",
    "normalize_batch",
    "resolve_source",
]
