"""Emitted actions of stapel-analytics (transactional outbox, at-least-once).

One topic, and it exists for one reason: server-side fan-out must not happen
on the ingest request thread (analytics-standard §3: delivery goes through
the outbox, never inline). Recording appends the rows and emits
``analytics.events.recorded`` inside the same transaction; whichever process
consumes Actions calls the adapters.

The payload carries the events themselves, which is the opposite of what
stapel-webhooks does with its delivery topics — and deliberately so. There
the payload had already reached the one subscriber entitled to it, so
re-broadcasting it would fan a stranger's data across the bus. Here the
events ARE the message: a fan-out consumer that had to re-read them from the
store would need the store's cursor, its retention window and its
credentials, and would still race the retention sweep.

The rows have already passed the PII guard and carry a hashed user id, never
a raw one — that is what makes the topic safe to put on a broker at all.

The GDPR topics this module also emits (``gdpr.section.erased``,
``gdpr.owner.alive``) are not here: they are stapel-gdpr's contract, emitted
from the handler that answers the request (``actions.py``), and their
schemas are committed verbatim in ``schemas/emits/``.
"""
from __future__ import annotations

EVENTS_RECORDED = "analytics.events.recorded"

#: Every topic this module emits from its own domain path.
EMITTED_EVENTS = (EVENTS_RECORDED,)

__all__ = ["EVENTS_RECORDED", "EMITTED_EVENTS"]
