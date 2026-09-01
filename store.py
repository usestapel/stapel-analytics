"""The storage seam — this module's whole relationship with the event store.

stapel-analytics owns no event table. Rows go to
``stapel_core.eventstore``, the fleet's append-only stream primitive (the
module-roadmap row says analytics writes through the eventstore seam; storage
design §1). That seam is already the design's "partitionable table, retention
is a setting": the default Postgres backend time-partitions the
raw table, and a deployment at scale routes the ``analytics`` stream to
another backend with one settings line and no release here::

    STAPEL_EVENTSTORE = {"ROUTES": {"analytics": "…ClickHouseEventStore"}}

Everything analytics-shaped therefore lives in the row's JSON payload
(``name``/``kind``/``props``/``anon_id``/``user_hash``/``session_id``/
``source``), which is also what makes subject-scoped erasure work: the store
purges by payload key, so "forget this person" is one call and not a
migration (see ``erasure.py``).

Keeping the seam in one small module is the point — no other file in this
package imports ``stapel_core.eventstore``, so the day a deployment needs a
different store, there is exactly one file to read.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Iterator, Mapping

logger = logging.getLogger(__name__)


def _rekey_unsupported():
    """The store's "this backend cannot re-key" type, re-exported.

    A caller has to be able to name the condition to handle it, and naming it
    by importing ``stapel_core.eventstore`` would put the store API back into
    a second file — the one thing this module exists to prevent (and the
    thing ``tests/test_store.py::TestSeamIsolation`` fails on). So the type
    comes through here, like everything else about the store does.
    """
    from stapel_core.eventstore import RekeyUnsupported

    return RekeyUnsupported


def __getattr__(name):
    """Lazy module attribute: ``store.RekeyUnsupported``.

    Lazy because importing the store API at module import time would make
    this package require Django's settings to be configured just to be
    imported, which ``append``/``query``/``purge`` all deliberately avoid.
    """
    if name == "RekeyUnsupported":
        return _rekey_unsupported()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def stream_name() -> str:
    """The event-store stream every analytics row is appended to."""
    from .conf import analytics_settings

    return str(analytics_settings.STREAM or "analytics")


def append(events: Iterable) -> int:
    """Append normalized events; return how many were buffered.

    Writes go through the store's buffer (batched by size/interval), so an
    ingest request never pays for an INSERT per event. A read flushes
    first, which is what makes a funnel report include the batch that was
    just accepted.
    """
    from stapel_core import eventstore

    stream = stream_name()
    rows = [
        eventstore.Event(stream=stream, payload=event.payload(), ts=event.ts)
        for event in events
    ]
    if not rows:
        return 0
    eventstore.append_batch(rows)
    return len(rows)


def flush() -> None:
    """Force-persist buffered appends (tests, management commands)."""
    from stapel_core import eventstore

    eventstore.flush()


def iter_events(
    *,
    time_range: tuple[datetime | None, datetime | None] | None = None,
    filters: Mapping[str, object] | None = None,
    limit: int | None = None,
) -> Iterator[dict]:
    """Walk stored rows oldest-first, paging through the store's cursor.

    Yields the raw payload dict with ``ts`` injected — the shape every
    consumer here (funnels, erasure, re-fan-out) actually wants. *limit*
    caps the total number of rows walked so a report can never become an
    unbounded scan triggered from a dashboard.
    """
    from stapel_core import eventstore

    from .conf import analytics_settings

    stream = stream_name()
    page_size = int(analytics_settings.QUERY_PAGE_SIZE or 1000)
    seen = 0
    cursor = None
    while True:
        remaining = page_size if limit is None else min(page_size, limit - seen)
        if remaining <= 0:
            return
        page = eventstore.query(
            stream,
            after=cursor,
            limit=remaining,
            time_range=time_range,
            filters=filters,
        )
        for event in page.events:
            row = dict(event.payload or {})
            row["ts"] = event.ts
            yield row
            seen += 1
        if not page.has_more:
            return
        cursor = page.cursor


def rollup(
    *,
    group_by,
    time_range: tuple[datetime | None, datetime | None] | None = None,
    filters: Mapping[str, object] | None = None,
) -> list:
    """Group-by counts over the stream — the dashboard's cheap half."""
    from stapel_core import eventstore

    return eventstore.rollup(
        stream_name(),
        group_by=list(group_by),
        sum_fields=[],
        time_range=time_range,
        filters=filters,
    )


def purge(
    *,
    older_than: datetime | None = None,
    filters: Mapping[str, object] | None = None,
) -> int:
    """Delete rows — retention (``older_than``) or erasure (``filters``).

    The store refuses an unbounded purge, and this does not try to talk it
    out of that: a call with neither bound would be "delete the analytics of
    everybody who ever used this deployment", which no caller here means.
    """
    from stapel_core import eventstore

    return eventstore.purge(stream_name(), older_than=older_than, filters=filters)


def rekey_subject(*, from_user_hash: str, to_user_hash: str) -> int:
    """Move one subject's whole history onto another subject key.

    The merge half of the identity story. ``erasure`` answers "forget this
    person"; this answers "these two hashes are one person" — a guest
    absorbed into the account it just proved it owns.

    One call, atomic in the store, idempotent under redelivery, and it
    appends nothing: see ``eventstore.rekey``. What it replaces — read-all,
    re-append under the new hash, purge the old — is three calls with no
    transaction across them, which under at-least-once delivery counts the
    moved history twice, permanently, in rows whose only purpose is to be
    counted.

    Raises :data:`RekeyUnsupported` if the deployment routed the analytics
    stream to a backend that cannot re-key; the caller decides what that
    means, exactly as it does for ``PurgeFiltersUnsupported``.
    """
    from stapel_core import eventstore

    return eventstore.rekey(
        stream_name(),
        field="user_hash",
        from_value=from_user_hash,
        to_value=to_user_hash,
    )


def uses_default_backend() -> bool:
    """Whether the analytics stream resolves to the built-in Postgres store.

    Asked by ``checks.py``: that backend keeps its rows in
    ``stapel_core.django.eventstore``'s table, so a host that mounted this
    module without adding that app to ``INSTALLED_APPS`` has an ingest
    endpoint that raises on every batch — and nothing else about the
    deployment looks wrong.
    """
    from stapel_core import eventstore
    from stapel_core.eventstore.backends.postgres import PostgresEventStore

    try:
        return isinstance(eventstore.resolve_backend(stream_name()), PostgresEventStore)
    except Exception:  # pragma: no cover - a broken backend is E-checked elsewhere
        return False


__all__ = [
    "append",
    "flush",
    "iter_events",
    "purge",
    "rollup",
    "stream_name",
    "uses_default_backend",
]
