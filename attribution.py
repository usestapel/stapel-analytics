"""The ad click that produced an account, read off a cookie the server owns.

**The gap this closes.** ``conversions.py`` can only report what somebody
handed it: a click identifier, and ideally the time of the click. Today that
identifier reaches the server exactly once — the frontend reads it off the
landing URL and posts it with the registration. That path is real and stays
first, but it is also the narrowest one in the funnel: it works only when
the visitor lands and registers in the same session, on the same device,
through a door the frontend controls. A visitor who lands on the marketing
site, leaves, and comes back a week later to sign up brings no ``?gclid=``
with them, and the campaign that paid for them is credited with nothing.

A cookie set on the paid landing closes that gap without asking the browser
to remember anything across origins: the marketing site writes
``<name>=base64url(json)`` on its apex domain, and every later request the
browser makes to the application — which is a subdomain of the same apex —
carries it. The application never has to be told; it only has to look.

**Why the server reads it rather than the frontend.** The cookie is
``HttpOnly``, which is the only reason it is safe to leave an advertising
identifier in a browser for ninety days: no script on any subdomain can
read it, exfiltrate it, or forge one. That makes the decode a server job by
construction, and it makes this module — not the frontend — the place the
rule lives.

**First touch wins, and that is a decision.** A stored attribution is never
overwritten by a cookie. The identifier already on the account came through
a narrower, more explicit door (a URL the visitor actually landed on, posted
by code we wrote) and it is the one a human would name if asked which click
produced the account. The cookie is the wide net underneath it. With
``FIRST_TOUCH`` off the newest click by ``clicked_at`` wins instead — which
is the other defensible answer, and the reason it is a setting rather than a
constant.

**Ninety days is the platform's rule, not ours, and it is enforced early.**
Google refuses a conversion whose click is older than
``GOOGLE_ADS_CONVERSION_WINDOW_DAYS``. A cookie lives ninety days by design,
so a click captured from one is routinely at the edge of that window and
sometimes past it. Such a record is stored anyway, with ``expired`` set:
deleting it would lose the only honest answer to "where did this account
come from", and storing it silently would let a conversion path enqueue an
upload that can only ever be settled ``expired``. The flag says which it is
before anything downstream has to ask.

**A malformed cookie is not an error.** It is somebody else's string —
written by a marketing site on a deploy cadence nobody here controls,
truncated by a proxy, or simply stale from a format that changed. Nothing it
can contain may fail a request, so every decode failure is counted
(``analytics.attribution_cookie.malformed``) and dropped. A metric is what
turns "the capture rate looks low" into an answer.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)

#: The click-identifier flavours this module will store. The same closed
#: list ``stapel_auth.attribution`` keeps, and closed for the same reason: an
#: identifier nobody can name a destination for is a column that never gets
#: uploaded anywhere. ``gclid``/``gbraid``/``wbraid`` reach Google Ads
#: through ``conversions.py`` and the feed; the rest are stored so the
#: record is complete and are filtered out of the Google feed by type.
CLICK_ID_TYPES = ("gclid", "gbraid", "wbraid", "yclid", "fbclid", "ttclid")

#: Longest click identifier accepted. Real values run ~100 characters; the
#: ceiling exists so an unbounded string cannot be posted into the column.
CLICK_ID_MAX_LENGTH = 512

#: The one format this module decodes today. A key rather than a boolean
#: because the next marketing site will hand us a different envelope, and a
#: setting that names the format is the seam that will take it.
FORMAT_BASE64URL_JSON = "base64url_json"

#: How a record got here. ``cookie`` is this module; a host writing the
#: record from its own registration path says so with its own word.
SOURCE_COOKIE = "cookie"

#: Metric names. Counted rather than logged per request: a malformed cookie
#: is a rate, and a log line per request would be a rate nobody can read.
METRIC_MALFORMED = "analytics.attribution_cookie.malformed"
METRIC_CAPTURED = "analytics.attribution_cookie.captured"
METRIC_EXPIRED = "analytics.attribution_cookie.expired"


# ── Configuration ────────────────────────────────────────────────────


def cookie_settings() -> dict:
    """``ATTRIBUTION_COOKIE`` merged over the shipped defaults.

    Merged, not replaced: a host that names only ``NAME`` — which is the
    whole configuration for a marketing site using the shipped envelope —
    must not silently lose ``FIELDS`` and end up decoding nothing.
    """
    from .conf import DEFAULTS, analytics_settings

    configured = analytics_settings.ATTRIBUTION_COOKIE or {}
    merged = {**DEFAULTS["ATTRIBUTION_COOKIE"], **dict(configured)}
    merged["FIELDS"] = {
        **DEFAULTS["ATTRIBUTION_COOKIE"]["FIELDS"],
        **dict(merged.get("FIELDS") or {}),
    }
    return merged


def cookie_name() -> str:
    """The cookie to look for. Empty = capture is off, and off means gone."""
    return str(cookie_settings().get("NAME") or "").strip()


def is_enabled() -> bool:
    """Whether cookie capture does anything at all on this deployment."""
    return bool(cookie_name())


def first_touch() -> bool:
    return bool(cookie_settings().get("FIRST_TOUCH", True))


def url_parameter() -> str:
    """Query parameter whose presence means the URL already said it.

    A request that carries the explicit attribution the frontend passes is
    not a request to capture a cookie on: the explicit value is about to be
    stored (or was, by the view this middleware wraps), and it wins.
    """
    return str(cookie_settings().get("URL_PARAM") or "").strip()


# ── The envelope ─────────────────────────────────────────────────────


def _counter(name: str, **labels) -> None:
    """Count, and never let the counting break the request."""
    try:
        from stapel_core.observability.metrics import counter

        counter(name, labels=labels or None)
    except Exception:  # pragma: no cover - observability is never load-bearing
        logger.debug("analytics: could not record metric %s", name, exc_info=True)


def decode(raw, *, settings_dict=None):
    """``{"click_id", "click_id_type", "clicked_at"}`` from a cookie value.

    ``None`` for anything unusable, with :data:`METRIC_MALFORMED` counted —
    see the module docstring on why a bad cookie cannot be an error.

    base64url without padding is what a JavaScript ``btoa`` + replace
    produces and what the spec here asks for, so the padding is restored
    here rather than demanded from the writer: a value whose length is not a
    multiple of four is the normal case, not a broken one.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    config = settings_dict or cookie_settings()
    if str(config.get("FORMAT") or "") != FORMAT_BASE64URL_JSON:
        logger.warning(
            "analytics: ATTRIBUTION_COOKIE FORMAT %r is not one this release "
            "decodes — the cookie is ignored",
            config.get("FORMAT"),
        )
        _counter(METRIC_MALFORMED, reason="format")
        return None

    payload = _payload(value)
    if payload is None:
        return None

    fields = config.get("FIELDS") or {}
    click_id = _text(payload.get(fields.get("id") or "id"))
    click_id_type = _text(payload.get(fields.get("type") or "type")).lower()
    clicked_at = _moment(payload.get(fields.get("ts") or "ts"))

    if not click_id or len(click_id) > CLICK_ID_MAX_LENGTH:
        _counter(METRIC_MALFORMED, reason="click_id")
        return None
    if click_id_type not in CLICK_ID_TYPES:
        # An identifier whose platform we cannot name has no destination:
        # an upload posts it to a named field or not at all.
        _counter(METRIC_MALFORMED, reason="click_id_type")
        return None
    if clicked_at is None:
        # Without the click time the 90-day rule falls back to the weaker
        # test (conversions.py), and the whole reason to read this cookie is
        # that it carries the real one.
        _counter(METRIC_MALFORMED, reason="ts")
        return None

    return {
        "click_id": click_id,
        "click_id_type": click_id_type,
        "clicked_at": clicked_at,
    }


def _payload(value: str):
    """The JSON object inside the envelope, or ``None`` (counted)."""
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError):
        _counter(METRIC_MALFORMED, reason="encoding")
        return None
    if not isinstance(payload, dict):
        _counter(METRIC_MALFORMED, reason="shape")
        return None
    return payload


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def _moment(value):
    """A unix-seconds stamp as an aware datetime, or ``None``.

    Seconds, not milliseconds: the envelope this decodes is written by a
    landing page doing ``Math.floor(Date.now()/1000)``. A millisecond value
    would land in the year 57000 and be stored as a click that never
    expires, so the far-future ones are refused rather than coerced — a
    guess about the unit is how a wrong click time becomes a wrong window.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0 or seconds > 1e11:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - platform
        return None


# ── The record ───────────────────────────────────────────────────────


def user_key(user):
    """The ``user_id`` a record is stored under, or ``None``.

    FK-less on purpose (models.py house rules): the accounts this library
    attributes may live in another service's database, and a row that
    outlives its account by exactly as long as an operator needs to see it
    is the fleet's precedent for advertising records.
    """
    pk = getattr(user, "pk", user)
    if pk in (None, ""):
        return None
    try:
        return uuid.UUID(str(pk))
    except (AttributeError, TypeError, ValueError):
        # Quiet: this is asked on every erasure too, and a host whose
        # accounts are not UUIDs would get a warning per deleted user for a
        # table it never writes to. The write path says it out loud instead.
        return None


def attribution_for(user):
    """The stored attribution of *user*, or ``None``.

    The accessor a host's conversion path calls instead of reaching for the
    model: it takes a user object or a bare id, and it is the one place that
    knows which is which.
    """
    from .models import UserAttribution

    key = user_key(user)
    if key is None:
        return None
    return UserAttribution.objects.filter(user_id=key).first()


def is_expired(clicked_at, *, now=None) -> bool:
    """Whether a click is already past the platform's reporting window."""
    from .conversions import window

    if clicked_at is None:
        return False
    return (now or timezone.now()) - clicked_at > window()


def record(user, decoded, *, source=SOURCE_COOKIE, now=None):
    """Store *decoded* against *user*. Returns ``(row, created)`` or ``None``.

    First touch wins: an existing row is returned untouched. With
    ``FIRST_TOUCH`` off it is replaced only by a strictly newer click —
    "newer" by ``clicked_at``, never by arrival order, because retries and a
    second tab reorder arrival and neither reorders the clock.
    """
    from .models import UserAttribution

    if not decoded:
        return None
    key = user_key(user)
    if key is None:
        logger.warning(
            "analytics: cannot store attribution for a non-UUID user id %r",
            getattr(user, "pk", user),
        )
        return None

    moment = now or timezone.now()
    clicked_at = decoded.get("clicked_at")
    fields = {
        "click_id": decoded["click_id"],
        "click_id_type": decoded["click_id_type"],
        "clicked_at": clicked_at,
        "captured_at": moment,
        "source": str(source or SOURCE_COOKIE)[:32],
        "expired": is_expired(clicked_at, now=moment),
    }
    row, created = UserAttribution.objects.get_or_create(
        user_id=key, defaults=fields
    )
    if created:
        _counter(METRIC_CAPTURED, click_id_type=fields["click_id_type"])
        if fields["expired"]:
            # Loud enough to find, quiet enough not to page: a click that
            # arrived already unreportable is a fact about the campaign's
            # latency, not a fault of this deployment.
            _counter(METRIC_EXPIRED, click_id_type=fields["click_id_type"])
            logger.info(
                "analytics: captured an attribution whose click (%s) is "
                "already past the reporting window — stored as expired",
                clicked_at.isoformat() if clicked_at else "unknown",
            )
        return row, True

    if first_touch():
        return row, False
    if clicked_at is None or row.clicked_at is None or clicked_at <= row.clicked_at:
        return row, False
    for name, value in fields.items():
        setattr(row, name, value)
    row.save(update_fields=[*fields, "updated_at"])
    return row, False


def capture(request, *, now=None):
    """Read the cookie off *request* and store it. The middleware's whole job.

    Returns the row when one was written, ``None`` in every other case —
    capture off, no cookie, no account, an attribution already stored, an
    explicit URL attribution on this very request, or a cookie that did not
    decode.
    """
    name = cookie_name()
    if not name:
        return None
    raw = request.COOKIES.get(name)
    if not raw:
        return None

    user = getattr(request, "user", None)
    # `is_authenticated` and nothing else: an anonymous *account* (a guest
    # enrolment) is an account, it can pay, and refusing to attribute it
    # would drop exactly the conversions this path exists to report.
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    parameter = url_parameter()
    if parameter and request.GET.get(parameter):
        # The explicit attribution the frontend passes is about to be stored
        # by the door this wraps, and it wins over the cookie.
        return None

    decoded = decode(raw)
    if decoded is None:
        return None

    written = record(user, decoded, now=now)
    if written is None:
        return None
    row, created = written
    return row if created else None


def attribution_as_dict(row):
    """One record as the plain mapping a conversion path reads."""
    if row is None:
        return None
    return {
        "user_id": str(row.user_id),
        "click_id": row.click_id,
        "click_id_type": row.click_id_type,
        "clicked_at": row.clicked_at.isoformat() if row.clicked_at else None,
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "source": row.source,
        "expired": row.expired,
    }


def erase_account(user_id) -> int:
    """Delete the attribution record of one account. Returns the row count.

    Called from ``erasure.py``: a click identifier is an advertising
    identifier of one person, and a module that stores one without a way out
    has built a personal-data store with no exit.
    """
    from .models import UserAttribution

    key = user_key(user_id)
    if key is None:
        return 0
    deleted, _detail = UserAttribution.objects.filter(user_id=key).delete()
    return int(deleted)


__all__ = [
    "CLICK_ID_MAX_LENGTH",
    "CLICK_ID_TYPES",
    "FORMAT_BASE64URL_JSON",
    "METRIC_CAPTURED",
    "METRIC_EXPIRED",
    "METRIC_MALFORMED",
    "SOURCE_COOKIE",
    "attribution_as_dict",
    "attribution_for",
    "capture",
    "cookie_name",
    "cookie_settings",
    "decode",
    "erase_account",
    "first_touch",
    "is_enabled",
    "is_expired",
    "record",
    "url_parameter",
    "user_key",
]
