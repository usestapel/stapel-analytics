"""The conversion feed — the outbox as a file the ad platform reads itself.

``conversions.py`` pushes: it holds credentials, calls somebody else's API
and owns a retry. This module does the same job with the arrow reversed —
it serves the outbox as a CSV over HTTPS and lets the platform's own data
manager fetch it on a schedule.

**Why a second delivery shape at all.** The push path needs an OAuth client,
a refresh token and a developer token that is granted per account and can be
refused; a deployment that cannot get one has no way to report an offline
conversion, and the conversions pile up in a table nobody drains. The pull
path needs none of that: a URL, a token, and a file. So the two are not
alternatives ranked by taste — they are the same fact offered through two
doors, and a host opens whichever one its ad account can walk through. Both
read the same rows, and a row served by the feed is *not* consumed: a pull
leaves no mark on what it read, and pretending otherwise would make the
file's contents depend on who fetched it last.

**Three properties decide the shape.**

1. **A re-read must be stable.** The puller decides its own schedule,
   retries on its own terms, and may fetch the same file twice. So the
   response is a pure function of (now, the outbox) over a window measured
   in days — ``CONVERSION_FEED_WINDOW_DAYS``, deliberately wider than the
   90-day click window — rather than a queue that drains as it is read. A
   conversion appears in many consecutive files and the platform
   deduplicates on (click id, conversion name, conversion time); a feed
   that removed a row once served would turn one missed fetch into one
   permanently lost conversion.

2. **The 90-day click window is enforced here, not hoped for.** A row whose
   click is older than ``GOOGLE_ADS_CONVERSION_WINDOW_DAYS`` is refused by
   the platform, so it never reaches the file — and, if it is still
   pending, it is settled ``expired`` by :func:`conversions.expire_stale`
   with a log line rather than being offered again tomorrow. This is the
   only scheduled thing a pull-only deployment runs, which is why the feed
   calls it: such a host never drains the outbox and would otherwise
   accumulate unreportable rows forever.

3. **An open feed is a data leak, so it ships closed.** The file carries
   click identifiers and payment values. ``CONVERSION_FEED_TOKEN`` is
   empty by default and an empty token means the endpoint does not exist —
   404, not "open" and not "401 with a hint". A presented token is compared
   in constant time, because a string comparison that returns early is a
   comparison that leaks the token one character at a time to anybody
   willing to time it.

**Three ways to present the token**, because the puller is somebody else's
scheduler and each one has a different form to fill in. ``Authorization:
Basic`` with the token as the password is the one Google's data manager
can send — its HTTPS connector offers a URL, a username and a password and
nothing else, so the username is ignored unless ``CONVERSION_FEED_USERNAME``
pins it. ``Authorization: Bearer`` is for a fetcher that can set a header.
``?token=`` is the weakest (a URL lands in access logs and referrers) and
exists for a fetcher that can only be given a URL; a host whose fetcher can
send a password must not put the token in the URL.

**The receipt.** A push knows it happened. A pull does not: the server
learns nothing from a schedule it does not run. :class:`~stapel_analytics.
models.ConversionFeedFetch` is one row per served response, and it exists
so that "has the first load happened yet" — the question that gates
switching a browser-side conversion to secondary — has an answer that is
not a guess about somebody else's cron.
"""
from __future__ import annotations

import base64
import binascii
import csv
import hmac
import io
import logging
from datetime import timedelta
from typing import NamedTuple

from django.utils import timezone

logger = logging.getLogger(__name__)

#: The column headers, in order. These follow the ad platform's
#: click-conversion file template: ``Google Click ID`` / ``Conversion
#: Name`` / ``Conversion Time`` / ``Conversion Value`` / ``Conversion
#: Currency``, plus the two iOS identifier columns.
#:
#: The three identifier columns are kept ADJACENT and first, because the
#: rule they express is "exactly one of these is filled" and a file that
#: shows that in its shape is a file an operator can check by eye. Nothing
#: downstream depends on the order: a data-manager connection maps columns
#: by header name in its wizard, which is also the place to compare these
#: spellings against the template the account is shown.
COLUMN_GCLID = "Google Click ID"
COLUMN_GBRAID = "GBRAID"
COLUMN_WBRAID = "WBRAID"
COLUMN_NAME = "Conversion Name"
COLUMN_TIME = "Conversion Time"
COLUMN_VALUE = "Conversion Value"
COLUMN_CURRENCY = "Conversion Currency"

COLUMNS = (
    COLUMN_GCLID,
    COLUMN_GBRAID,
    COLUMN_WBRAID,
    COLUMN_NAME,
    COLUMN_TIME,
    COLUMN_VALUE,
    COLUMN_CURRENCY,
)

#: Which column a click id lands in, by its kind. The same mapping
#: ``conversions.CLICK_ID_FIELDS`` makes for the API, in the file's
#: vocabulary — and made once, so one test can assert the whole thing.
CLICK_ID_COLUMNS = {
    "gclid": COLUMN_GCLID,
    "gbraid": COLUMN_GBRAID,
    "wbraid": COLUMN_WBRAID,
}

#: Statuses the feed offers. ``pending`` is the point of the file; a row
#: the push path already ``uploaded`` is offered again because the platform
#: deduplicates and a host may run both doors. ``rejected`` and ``skipped``
#: are settled refusals — re-offering them would ask the same question that
#: was already answered no.
FED_STATUSES = ("pending", "uploaded")

#: How much receipt history :func:`record_fetch` keeps, in days. A constant
#: rather than a setting: the table answers "when was it last read, and how
#: has that been going", and no deployment needs a different answer to it.
FETCH_HISTORY_DAYS = 90

#: The shipped placeholder for ``CONVERSION_FEED_CONVERSION_NAME``. Left in
#: place it produces a file that imports zero rows, so ``analytics.W012``
#: names it at boot when the feed is switched on.
PLACEHOLDER_CONVERSION_NAME = "Offline conversion"


# ── Configuration ────────────────────────────────────────────────────


def token() -> str:
    """The configured feed token, stripped. Empty = the feed is off."""
    from .conf import analytics_settings

    return str(analytics_settings.CONVERSION_FEED_TOKEN or "").strip()


def is_enabled() -> bool:
    """Whether the endpoint exists at all. No token, no endpoint."""
    return bool(token())


def token_matches(presented) -> bool:
    """Constant-time comparison against the configured token.

    ``hmac.compare_digest`` rather than ``==``: an early-returning compare
    tells a patient caller how many leading characters they got right, and
    the thing being guarded is a file of click identifiers and payment
    values. Returns ``False`` when the feed is off, so a disabled feed
    cannot be talked into a match by presenting an empty token.
    """
    configured = token()
    if not configured:
        return False
    return hmac.compare_digest(str(presented or ""), configured)


class Credential(NamedTuple):
    """What a request presented, and through which door.

    ``scheme`` is one of :data:`SCHEMES` (or ``""`` when nothing was
    presented); it is what the fetch log line names, so a host can tell
    "Google's connector fetched it" from "somebody curled it" without the
    secret ever being written down.
    """

    scheme: str
    token: str
    username: str = ""


#: The schemes, by the name the log line and the tests use.
SCHEME_BASIC = "basic"
SCHEME_BEARER = "bearer"
SCHEME_QUERY = "query"
SCHEMES = (SCHEME_BASIC, SCHEME_BEARER, SCHEME_QUERY)

#: The challenge a refusal carries. RFC 7235: a 401 without one is a
#: response a client is not allowed to retry with credentials, and a data
#: manager's fetcher may well insist on seeing it before sending any.
CHALLENGE = 'Basic realm="conversions feed"'


def username() -> str:
    """The pinned Basic-auth username, stripped. Empty = any username."""
    from .conf import analytics_settings

    return str(analytics_settings.CONVERSION_FEED_USERNAME or "").strip()


def _decode_basic(value: str) -> tuple[str, str] | None:
    try:
        raw = base64.b64decode(value.strip().encode("ascii"), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        return None
    if ":" not in text:
        return None
    user, _, password = text.partition(":")
    return user, password


def presented_credential(request) -> Credential:
    """The credential this request carries and the scheme it came through.

    ``Authorization: Basic`` is decoded into (username, password) and the
    PASSWORD is the token — the shape Google's data manager can send. A
    header that is not Basic is read as a bearer: ``Bearer``/``Token`` with
    a value, or the whole header as the token for a fetcher that sends it
    bare. Only when there is no header at all is ``?token=`` consulted, so
    a URL that carries a token cannot be overridden by a stray header.
    """
    header = str(request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "basic":
            decoded = _decode_basic(value)
            if decoded is None:
                return Credential(SCHEME_BASIC, "", "")
            user, password = decoded
            return Credential(SCHEME_BASIC, password, user)
        if scheme.lower() in ("bearer", "token") and value.strip():
            return Credential(SCHEME_BEARER, value.strip())
        return Credential(SCHEME_BEARER, header)
    query = str(request.GET.get("token") or "").strip()
    if query:
        return Credential(SCHEME_QUERY, query)
    return Credential("", "")


def presented_token(request) -> str:
    """The token this request carries, whichever door it came through."""
    return presented_credential(request).token


def credential_matches(credential: Credential) -> bool:
    """Whether *credential* opens the feed.

    The token must match (constant time, :func:`token_matches`). The
    username is only checked when ``CONVERSION_FEED_USERNAME`` pins one, and
    only for Basic — a bearer has no username to check. Both comparisons run
    whatever the first one said, so a wrong username does not answer faster
    than a wrong password.
    """
    token_ok = token_matches(credential.token)
    pinned = username()
    if pinned and credential.scheme == SCHEME_BASIC:
        user_ok = hmac.compare_digest(str(credential.username or ""), pinned)
    else:
        user_ok = True
    return token_ok and user_ok


def user_agent(request) -> str:
    return str(request.META.get("HTTP_USER_AGENT") or "").strip()[:200]


def log_fetch(request, *, scheme: str, status: int, rows: int | None = None) -> None:
    """One INFO line per fetch attempt — the scheme, never the credential.

    This is how a host learns that the first successful fetch by the
    platform's connector has happened, which is what gates switching a
    browser-side conversion goal to secondary. The receipt table records
    served responses; the log also records refusals, because "Google is
    hitting the URL and getting 401" is a configuration mistake a host
    should see in the same place.
    """
    logger.info(
        "conversion feed: scheme=%s status=%s rows=%s remote=%s user_agent=%r",
        scheme or "none",
        status,
        "-" if rows is None else rows,
        remote_address(request) or "-",
        user_agent(request) or "-",
    )


def window_days() -> int:
    from .conf import analytics_settings

    return int(analytics_settings.CONVERSION_FEED_WINDOW_DAYS)


def conversion_name() -> str:
    """The ``Conversion Name`` every row carries.

    One name for the whole feed, not one per row: the file matches the
    conversion action by its DISPLAY name in the ads account, and that name
    is a property of the account this deployment reports to — the same
    thing ``GOOGLE_ADS_CONVERSION_ACTION_PAID``-style resource names say in
    the API's vocabulary.
    """
    from .conf import analytics_settings

    return str(analytics_settings.CONVERSION_FEED_CONVERSION_NAME or "").strip()


# ── The rows ─────────────────────────────────────────────────────────


def feed_rows(*, now=None):
    """The outbox rows this feed serves, oldest conversion first.

    Two bounds, and they are different in kind. The feed window is ours: it
    says how much history one response carries. The click window is the
    platform's: a row past it is refused on import, so it is filtered out
    here whatever its status — including a row the push path already
    uploaded, which was fine when it went and is not fine to re-offer now.
    """
    from .conversions import stale_verdict
    from .models import ConversionUpload

    moment = now or timezone.now()
    since = moment - timedelta(days=window_days())
    queryset = (
        ConversionUpload.objects.filter(
            status__in=FED_STATUSES, conversion_at__gte=since
        )
        .exclude(click_id="")
        .order_by("conversion_at", "pk")
    )
    return [row for row in queryset if stale_verdict(row, now=moment) is None]


def row_values(row) -> dict:
    """One outbox row as the file's columns.

    The click id goes in the column its KIND names and the other two stay
    empty — a gbraid written into the gclid column is not a mistyped value,
    it is a row the platform drops.
    """
    values = dict.fromkeys(COLUMNS, "")
    column = CLICK_ID_COLUMNS.get(row.click_id_type)
    if column is None:
        raise ValueError(f"unknown click id type {row.click_id_type!r}")
    from .conversions import google_datetime

    values[column] = row.click_id
    values[COLUMN_NAME] = conversion_name()
    values[COLUMN_TIME] = google_datetime(row.conversion_at)
    if row.value is not None:
        values[COLUMN_VALUE] = f"{row.value:f}".rstrip("0").rstrip(".") or "0"
        values[COLUMN_CURRENCY] = row.currency
    return values


def render_csv(rows) -> str:
    """The whole response body, headers included.

    ``\\r\\n`` line endings and minimal quoting — the CSV dialect the
    template is written in. A header-only body is the correct answer to an
    empty outbox: it says "the feed works and there is nothing to report",
    which an empty file does not.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(COLUMNS), lineterminator="\r\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row_values(row))
    return buffer.getvalue()


# ── The receipt ──────────────────────────────────────────────────────


def remote_address(request) -> str:
    """Best-effort caller address. Blank rather than guessed."""
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return str(request.META.get("REMOTE_ADDR") or "").strip()[:64]


def record_fetch(*, rows: int, remote: str = ""):
    """Write down that the feed was served, and trim the old receipts."""
    from .models import ConversionFeedFetch

    fetch = ConversionFeedFetch.objects.create(rows=int(rows), remote=remote[:64])
    ConversionFeedFetch.objects.filter(
        at__lt=timezone.now() - timedelta(days=FETCH_HISTORY_DAYS)
    ).delete()
    return fetch


def feed_status() -> dict:
    """What an operator asks: is it on, was it read, and what was in it.

    ``fetches`` counts only what the receipt table still holds
    (:data:`FETCH_HISTORY_DAYS`), and says so by name rather than
    pretending to be a lifetime total.
    """
    from .models import ConversionFeedFetch, ConversionUpload

    last = ConversionFeedFetch.objects.order_by("-at").first()
    return {
        "enabled": is_enabled(),
        "conversion_name": conversion_name(),
        "window_days": window_days(),
        "rows_available": len(feed_rows()),
        "pending": ConversionUpload.objects.filter(
            status=ConversionUpload.STATUS_PENDING
        ).count(),
        "fetches": ConversionFeedFetch.objects.count(),
        "last_fetch_at": last.at.isoformat() if last else None,
        "last_fetch_rows": last.rows if last else None,
        "last_fetch_remote": last.remote if last else None,
    }


__all__ = [
    "CLICK_ID_COLUMNS",
    "COLUMNS",
    "FED_STATUSES",
    "FETCH_HISTORY_DAYS",
    "PLACEHOLDER_CONVERSION_NAME",
    "conversion_name",
    "feed_rows",
    "feed_status",
    "CHALLENGE",
    "Credential",
    "SCHEMES",
    "SCHEME_BASIC",
    "SCHEME_BEARER",
    "SCHEME_QUERY",
    "credential_matches",
    "is_enabled",
    "log_fetch",
    "presented_credential",
    "presented_token",
    "record_fetch",
    "remote_address",
    "render_csv",
    "row_values",
    "token",
    "token_matches",
    "user_agent",
    "username",
    "window_days",
]
