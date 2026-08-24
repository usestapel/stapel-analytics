"""The PII guard — the backend half of analytics-standard §1.4.

The frontend facade already redacts PII-shaped prop VALUES before a batch
leaves the browser (``@stapel/analytics``' ``pii.ts``). This is the
backstop, and it exists because the frontend guard is advice: anyone can
POST to the ingest endpoint, and an app-layer server module calling
``track()`` never went through the browser at all.

The heuristics are deliberately the same two the frontend uses — an email
shape and a phone shape, judged on VALUES and never on keys. Keys are not
judged because ``{"email_verified": true}`` is not PII and
``{"note": "call me at 555-0100"}`` is; the value is where the person is.

Default mode is ``reject``: the event does not enter the store and the
response says which prop refused it. "Store it and warn" is the wrong
default for a table that is personal data by construction — a warning nobody
reads is how a phone number ends up in a five-year retention window.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Same expressions as ``@stapel/analytics``' ``pii.ts``, so a value the
#: browser would have redacted is a value this refuses.
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[a-zA-Z]{2,}")
_PHONE_SHAPE_RE = re.compile(r"^\+?[\d\s\-().]{7,}$")

#: One deliberate, one-directional refinement over the frontend heuristic:
#: an ISO-8601 date or timestamp matches the phone SHAPE (``2026-08-24`` is
#: eight digits and two dashes) and is not a phone number. The frontend
#: redacts such a value, so this module never sees it from a browser; where
#: it is reached — ``track()`` from a server module, a non-facade producer —
#: refusing it would drop a legitimate event under the default ``reject``
#: mode. Leniency in this direction is safe (the browser is still strict);
#: the reverse would not be. The frontend guard should adopt the same
#: exemption — filed in MODULE.md §10.
_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)

#: What ``strip`` mode leaves behind. Byte-identical to the frontend's
#: ``PII_REDACTED`` so a redacted value reads the same wherever it happened.
PII_REDACTED = "[redacted]"


class PiiRefused(Exception):
    """A prop value looked like PII and the mode is ``reject``.

    Carries the dotted path of the offending prop so the refusal can name it
    ("props.contact.phone") instead of saying "something in there".
    """

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


def looks_like_pii(value: str) -> bool:
    """The default heuristic, swappable through ``PII_GUARD``."""
    if _EMAIL_RE.search(value):
        return True
    trimmed = value.strip()
    if _ISO_DATE_RE.match(trimmed):
        return False
    if not _PHONE_SHAPE_RE.match(trimmed):
        return False
    return len(re.sub(r"\D", "", trimmed)) >= 7


def pii_mode() -> str:
    """``"reject"`` | ``"strip"`` | ``"warn"`` | ``"off"``."""
    from .conf import analytics_settings

    mode = str(analytics_settings.PII_MODE or "reject").lower()
    return mode if mode in ("reject", "strip", "warn", "off") else "reject"


def _guard() -> callable:
    from .conf import analytics_settings

    return analytics_settings.PII_GUARD


def guard_props(props, *, event_name: str, mode: str | None = None):
    """Return *props* with PII handled per *mode*; raise in ``reject`` mode.

    Recurses through nested objects and arrays: the frontend guard does, and
    a guard that stops at the first level is a guard somebody routes around
    by nesting one dict deeper.
    """
    mode = mode or pii_mode()
    if mode == "off" or not props:
        return props
    guard = _guard()

    def walk(value, path: str):
        if isinstance(value, str):
            if not guard(value):
                return value
            if mode == "reject":
                raise PiiRefused(path)
            logger.warning(
                "analytics: PII-shaped value at %s of event %r (%s)",
                path, event_name, "redacted" if mode == "strip" else "kept",
            )
            return PII_REDACTED if mode == "strip" else value
        if isinstance(value, list):
            return [walk(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, tuple):
            return [walk(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: walk(item, f"{path}.{key}") for key, item in value.items()}
        return value

    return walk(props, "props")


def hash_user_id(user_id) -> str:
    """``sha256(salt + str(user_id))`` in hex — the subject key of a person.

    Unsalted by default, and that is a wire-compatibility decision rather
    than an oversight: ``@stapel/analytics``' ``hash.ts`` computes
    ``sha256Hex(userId)`` in the browser, so an unsalted server hash is the
    only way a bridged server step (``payment.completed``) lands on the same
    funnel subject as the clicks that preceded it. ``USER_HASH_SALT`` makes
    the store unlinkable to an id guesser and breaks that join; ``W008``
    states the trade at boot.
    """
    import hashlib

    from .conf import analytics_settings

    salt = str(analytics_settings.USER_HASH_SALT or "")
    return hashlib.sha256((salt + str(user_id)).encode("utf-8")).hexdigest()


__all__ = [
    "PII_REDACTED",
    "PiiRefused",
    "guard_props",
    "hash_user_id",
    "looks_like_pii",
    "pii_mode",
]
