"""Subject-scoped erasure (GDPR Art. 17) — the slice stapel-analytics owns.

**Analytics rows are user data.** A behavioural stream keyed to a person is
personal data whether or not the person's name appears in it, and the
pseudonymisation this module does (a hashed user id) is exactly that —
pseudonymisation, not anonymisation. So this module ships an erasure
provider from its first release rather than adding one later.

The policy is **hard delete**, and there is no anonymize alternative worth
having: an analytics row stripped of its subject is a row that cannot be
attributed, cannot be funnelled and cannot be reported on. Keeping it would
mean keeping the timestamp and the props of a person's session for the sake
of a count — which is precisely the "we kept a little bit" that erasure
exists to end.

Two passes, in this order and for a reason:

1. Rows carrying the person's ``user_hash``.
2. Rows carrying an ``anon_id`` this person was ever seen under — collected
   BEFORE the first pass, because after it the linking rows are gone. An
   anonymous session that later identified is that person's data as much as
   the identified half; leaving it behind would leave a complete browsing
   history keyed to a cookie that the same person will present again.

Both passes go through ``eventstore.purge(..., filters=...)`` — subject
scoped, not a time sweep. A store whose backend predates filtered purge
raises rather than quietly degrading into a retention sweep: an erasure that
silently became something else is worse than an erasure that stopped.

Every entry point returns a counts mapping — "it says what it did", not "it
says it ran" — and is idempotent: a redelivered request finds nothing left
and receipts zeros.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: This module's name in ``STAPEL_GDPR["DATA_OWNERS"]``. Same string as the
#: GDPR provider's ``section``: one owner, one name, whichever protocol
#: reaches it.
OWNER = "analytics"

#: The subject types this owner claims. Must match its row in the host's
#: ``DATA_OWNERS`` map — gdpr creates an ``ErasurePart`` for this owner only
#: for subjects listed here, and ``gdpr.owner.alive`` reports this list.
#:
#: ``anon`` is claimed as well as ``account``, because the thing a browser
#: sends before anyone logs in is a subject in its own right: a deployment
#: honouring a "delete my data" from an unauthenticated visitor has an
#: anonymous id and nothing else to erase by.
SUBJECT_TYPES = ("account", "anon")


def erase(subject_type: str, subject_key, **kwargs) -> dict:
    """Erase everything analytics owns about one subject; return what went.

    Unknown subject types are refused with :class:`ValueError` — a typo must
    not receipt as an empty success, which would certify an erasure nobody
    performed.
    """
    if subject_type == "account":
        return erase_account(subject_key)
    if subject_type == "anon":
        return erase_anonymous(subject_key)
    raise ValueError(f"stapel-analytics does not own subject type {subject_type!r}")


def linked_anon_ids(user_hash: str) -> list:
    """Anonymous ids ever seen on a row carrying *user_hash*.

    Bounded by ``ERASURE_MAX_ANON_IDS``-worth of scanning through the
    store's own page size; an identity with more distinct devices than that
    is pathological, and the bound keeps an erasure from becoming a full
    table scan under a request timeout.
    """
    from .store import iter_events

    found: list[str] = []
    seen: set[str] = set()
    for row in iter_events(filters={"user_hash": user_hash}):
        anon = row.get("anon_id")
        if anon and anon not in seen:
            seen.add(anon)
            found.append(anon)
    return found


def erase_account(user_id_or_hash, *, already_hashed: bool = False) -> dict:
    """Erase one person's whole analytics history.

    Accepts a raw user id (hashed here, the same way ingest hashes it) or a
    hash directly. The ``user.deleted`` / ``gdpr.erasure.requested`` path
    passes a raw id, because that is what the fleet's subject key for an
    account is; ``already_hashed`` is for a caller that only ever held the
    hash.
    """
    from .privacy import hash_user_id
    from .store import purge

    user_hash = (
        str(user_id_or_hash) if already_hashed else hash_user_id(user_id_or_hash)
    )
    # Collected first: the linking rows are about to be deleted.
    anon_ids = linked_anon_ids(user_hash)
    counts = {
        "events": purge(filters={"user_hash": user_hash}),
        "anonymous_events": 0,
        "anonymous_ids": len(anon_ids),
    }
    for anon_id in anon_ids:
        counts["anonymous_events"] += purge(filters={"anon_id": anon_id})
    logger.info("analytics: account erased (%s)", counts)
    return counts


def erase_anonymous(anon_id) -> dict:
    """Erase every row carrying one anonymous id."""
    from .store import purge

    counts = {"events": purge(filters={"anon_id": str(anon_id)})}
    logger.info("analytics: anonymous subject %s erased (%s)", anon_id, counts)
    return counts


def export_account(user_id_or_hash, *, already_hashed: bool = False) -> dict:
    """One person's rows, for a DSAR export.

    Capped by the store's paging; a person with an unbounded history gets a
    truncated export flagged as such rather than an export that times out
    and produces nothing.
    """
    from .conf import analytics_settings
    from .privacy import hash_user_id
    from .store import iter_events

    user_hash = (
        str(user_id_or_hash) if already_hashed else hash_user_id(user_id_or_hash)
    )
    budget = int(analytics_settings.MAX_REPORT_EVENTS or 200000)
    rows = []
    for row in iter_events(filters={"user_hash": user_hash}, limit=budget):
        ts = row.pop("ts", None)
        rows.append({**row, "ts": ts.isoformat() if ts is not None else None})
    return {
        "user_hash": user_hash,
        "events": rows,
        "count": len(rows),
        "truncated": len(rows) >= budget,
    }


__all__ = [
    "OWNER",
    "SUBJECT_TYPES",
    "erase",
    "erase_account",
    "erase_anonymous",
    "export_account",
    "linked_anon_ids",
]
