"""Offline click-conversion upload to Google Ads.

The gap this closes. A click is measured in the browser, the conversion it
led to happens somewhere the browser is not — a phone call, a contract
signed a week later, a refund. Google Ads calls that an *offline
conversion*: you hand back the click identifier the ad platform gave you
(``gclid``, or the privacy-preserving ``gbraid`` / ``wbraid`` that replaced
it for iOS app↔web journeys) together with what the click was eventually
worth, and the bidding learns from outcomes instead of from form
submissions.

Three properties decide the shape of this module.

1. **The upload is somebody else's API.** It fails for reasons that have
   nothing to do with the conversion: an expired refresh token, a quota, a
   network. So the fact is written down first (``ConversionUpload``) and
   uploaded second, and a failure that is not a verdict is retried rather
   than lost. This is the same outbox discipline the rest of the module
   uses for fan-out, made durable — unlike a fan-out mirror there is no
   second copy to reconcile from.

2. **Delivery must be exactly-once-ish from the caller's side.** Google
   deduplicates a repeated (click id, conversion action, time) itself, but
   an upload that runs twice still burns quota and still reports twice in
   the API's own logs. ``(click_id, conversion_action, conversion_at)`` is
   unique here and ``enqueue`` is a ``get_or_create``, so a retried
   webhook, a replayed Action or an operator re-running an importer all
   converge on one row and at most one upload.

3. **The SDK is optional.** ``google-ads`` is a large dependency with a
   protobuf runtime; a library that made every host install it to import
   ``stapel_analytics.models`` would be a library nobody mounts. The import
   happens inside :func:`_client_class`, at call time, and every code path
   above it works without the package installed — which is also what makes
   the tests here stub the client instead of reaching the network.

**The 90-day window, and what this module can honestly enforce.** Google
refuses a conversion whose click is more than ``GOOGLE_ADS_CONVERSION_
WINDOW_DAYS`` (90) old. That distance is *click → conversion*, and the
click time is not something a conversion event carries. So:

- when the caller supplies ``clicked_at``, the real rule is enforced:
  ``conversion_at - clicked_at > window`` is skipped as ``window``;
- when it does not, the module falls back to ``now - conversion_at >
  window``. That is a strictly weaker test — a conversion older than the
  window implies a click older than the window, so the fallback never
  skips a conversion Google would have taken, but it does let through
  conversions Google will reject with its own message. Those come back
  ``rejected`` with Google's reason, not silently.

Supplying ``clicked_at`` is therefore the difference between a local
refusal and a wasted API call, and this module does not pretend otherwise.

**And a row can run out of time while it waits.** The two verdicts are not
the same question. ``window_verdict`` asks whether the conversion happened
too long after its click — a fact about the pair, true the moment it was
written down. :func:`expiry_verdict` asks whether the *click* has aged past
the window as of now: the pair was reportable when it arrived and is not
reportable any more, because nobody reported it. That is our latency, not
the caller's data, and it comes back ``expired`` rather than ``window`` so
a backlog can be read for what it is. Both are terminal — nothing later
makes a click younger — so :func:`expire_stale` settles them with a log
line instead of leaving a queue that retries the unreportable forever.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The request field each click-id kind travels in. Google models them as
#: three distinct fields on ``ClickConversion``, not as a value plus a type
#: discriminator: a gbraid written to ``gclid`` is a rejected upload, not a
#: mismatched string. Keeping the map here (rather than an ``if`` ladder in
#: the builder) is what lets one test assert the whole mapping.
CLICK_ID_FIELDS = {
    "gclid": "gclid",
    "gbraid": "gbraid",
    "wbraid": "wbraid",
}

#: Settings that must ALL be present before an upload can be attempted.
#: ``GOOGLE_ADS_LOGIN_CUSTOMER_ID`` is deliberately absent: it is only
#: needed when the calling account is a manager (MCC), and demanding it
#: from a direct advertiser would make a correct configuration look broken.
REQUIRED_CREDENTIALS = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
)

#: Reason codes this module produces itself. A rejection reason comes from
#: Google and is passed through verbatim.
REASON_WINDOW = "window"
#: The click itself has aged out of the window as of NOW. Distinct from
#: ``window`` on purpose: ``window`` says the conversion happened too long
#: after its click (a fact about the pair, true the moment it was
#: enqueued), while ``expired`` says the pair was fine and we ran out of
#: time to report it. One is a caller's data, the other is our own
#: latency, and an operator reading a backlog needs to tell them apart.
REASON_EXPIRED = "expired"
REASON_NOT_CONFIGURED = "not_configured"
REASON_NO_RESULT = "no_result"
REASON_MAX_ATTEMPTS = "max_attempts"

#: Outcome statuses. ``pending`` is the honest fourth answer: the upload
#: was neither done, refused by Google, nor skipped — it could not be
#: ATTEMPTED (transport, quota, an outage), the row is durable, and the
#: command will try again. Reporting any of the other three would be a lie
#: about a row that is still going to move.
STATUS_UPLOADED = "uploaded"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"
STATUS_PENDING = "pending"


class GoogleAdsUnavailable(Exception):
    """The upload could not be attempted. Retryable, not a verdict."""


# ── Configuration ────────────────────────────────────────────────────


def credentials() -> dict:
    """The ``GoogleAdsClient.load_from_dict`` mapping, or ``{}`` if incomplete.

    Empty rather than partial on purpose: a client built from four of five
    credentials fails deep inside the SDK with a message about OAuth, hours
    after the deploy that dropped the fifth.
    """
    from .conf import analytics_settings

    values = {
        name: str(getattr(analytics_settings, name) or "").strip()
        for name in REQUIRED_CREDENTIALS
    }
    if not all(values.values()):
        return {}
    config = {
        "developer_token": values["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": values["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": values["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": values["GOOGLE_ADS_REFRESH_TOKEN"],
        "use_proto_plus": True,
    }
    login_customer_id = str(
        analytics_settings.GOOGLE_ADS_LOGIN_CUSTOMER_ID or ""
    ).strip()
    if login_customer_id:
        config["login_customer_id"] = _digits(login_customer_id)
    return config


def is_configured() -> bool:
    """Whether an upload can be attempted at all."""
    return bool(credentials())


def customer_id() -> str:
    """The advertiser account uploads are written to, digits only."""
    from .conf import analytics_settings

    return _digits(str(analytics_settings.GOOGLE_ADS_CUSTOMER_ID or ""))


def _digits(value: str) -> str:
    """Google customer ids are written ``123-456-7890`` and sent ``1234567890``."""
    return "".join(character for character in value if character.isdigit())


def window() -> timedelta:
    from .conf import analytics_settings

    return timedelta(days=int(analytics_settings.GOOGLE_ADS_CONVERSION_WINDOW_DAYS))


# ── Formatting ───────────────────────────────────────────────────────


def parse_timestamp(raw):
    """ISO-8601 in, aware ``datetime`` out. ``None``/empty stays ``None``.

    A naive stamp is read in the deployment's default timezone rather than
    refused: the caller that has one is usually a back-office importer
    reading a spreadsheet, and losing the conversion over a missing ``Z``
    would be the wrong trade. What is never guessed is the OFFSET SENT to
    Google — see :func:`google_datetime`.
    """
    if raw in (None, ""):
        return None
    from datetime import datetime

    if isinstance(raw, datetime):
        parsed = raw
    else:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


def google_datetime(value) -> str:
    """Google's ``conversion_date_time``: ``yyyy-mm-dd hh:mm:ss+|-hh:mm``.

    The offset is mandatory — the API refuses a bare local timestamp, and
    it is right to: a conversion time without an offset is a number whose
    meaning depends on which account read it.
    """
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_default_timezone())
    offset = value.strftime("%z") or "+0000"
    return f"{value.strftime('%Y-%m-%d %H:%M:%S')}{offset[:3]}:{offset[3:]}"


# ── The window ───────────────────────────────────────────────────────


def window_verdict(conversion_at, clicked_at=None, *, now=None) -> str | None:
    """``"window"`` when this conversion is past Google's horizon, else ``None``.

    Measured click→conversion when ``clicked_at`` is known and now→
    conversion otherwise; the module docstring says what the fallback can
    and cannot prove.
    """
    horizon = window()
    if clicked_at is not None:
        return REASON_WINDOW if conversion_at - clicked_at > horizon else None
    reference = now or timezone.now()
    return REASON_WINDOW if reference - conversion_at > horizon else None


def expiry_verdict(conversion_at, clicked_at=None, *, now=None) -> str | None:
    """``"expired"`` when the CLICK has aged out of the window as of now.

    ``window_verdict`` asks whether the conversion came too long after its
    click. This asks the other question, the one that only time can answer:
    the pair was reportable when it was written down, and it is not
    reportable any more. Google measures the 90 days from the click, so the
    reference is ``clicked_at`` when the caller supplied it and
    ``conversion_at`` otherwise — the same weaker fallback the module
    docstring describes, and never a stricter one.

    Terminal by construction: nothing that happens later makes a click
    younger, so a row this refuses is settled rather than retried.
    """
    reference = clicked_at if clicked_at is not None else conversion_at
    moment = now or timezone.now()
    return REASON_EXPIRED if moment - reference > window() else None


def stale_verdict(row, *, now=None) -> str | None:
    """Both permanent refusals for one outbox row, in order of specificity.

    ``window`` first: it is the more precise statement about a row that
    fails both, and it was true before ``expired`` became true.
    """
    return window_verdict(row.conversion_at, row.clicked_at, now=now) or expiry_verdict(
        row.conversion_at, row.clicked_at, now=now
    )


def expire_stale(*, now=None) -> int:
    """Settle every pending row that can never be uploaded. Returns the count.

    This is the half of the outbox that has no other owner. A row whose
    click has aged out is not "failing" — it is *finished*, and a queue
    that keeps re-attempting it burns quota, hides the rows behind it and
    turns a bounded backlog into an unbounded one. Each settled row gets a
    log line, because a conversion this deployment could have reported and
    did not is a fact somebody should be able to find.

    Called from both places that touch the outbox on a schedule — the
    upload task and the feed — precisely because a deployment may run only
    one of them. A host with no Google Ads credentials never drains, and a
    host whose puller reads the feed never uploads; neither may accumulate
    rows forever.

    Idempotent: settled rows are terminal, so a second pass finds nothing.
    """
    from django.db.models import Q

    from .models import ConversionUpload

    moment = now or timezone.now()
    horizon = moment - window()
    # Narrow in the database first — the exact verdict needs both columns
    # and this only has to be a superset of the rows it can settle.
    candidates = ConversionUpload.objects.filter(
        status=ConversionUpload.STATUS_PENDING
    ).filter(
        Q(clicked_at__lt=horizon)
        | (Q(clicked_at__isnull=True) & Q(conversion_at__lt=horizon))
    )
    settled = 0
    for row in candidates.iterator():
        reason = stale_verdict(row, now=moment)
        if reason is None:  # pragma: no cover — the filter is a superset
            continue
        _settle(row, ConversionUpload.STATUS_SKIPPED, reason)
        logger.info(
            "conversion upload %s settled as %s: click %s, conversion %s, "
            "window %s day(s) — it will not be retried",
            row.pk,
            reason,
            row.clicked_at.isoformat() if row.clicked_at else "unknown",
            row.conversion_at.isoformat(),
            window().days,
        )
        settled += 1
    return settled


# ── The SDK seam ─────────────────────────────────────────────────────


def _client_class():
    """The ``GoogleAdsClient`` class, imported at call time.

    The one place the optional dependency is named. Tests replace this
    function; a host without the ``[google-ads]`` extra never reaches it.
    """
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError as exc:  # pragma: no cover — depends on the profile
        raise GoogleAdsUnavailable(
            "the google-ads SDK is not installed — "
            "pip install 'stapel-analytics[google-ads]'"
        ) from exc
    return GoogleAdsClient


def build_client():
    """A configured ``GoogleAdsClient``, or raise ``GoogleAdsUnavailable``."""
    from .conf import analytics_settings

    config = credentials()
    if not config:
        raise GoogleAdsUnavailable("Google Ads credentials are incomplete")
    version = analytics_settings.GOOGLE_ADS_API_VERSION
    client_class = _client_class()
    if version:
        return client_class.load_from_dict(config, version=str(version))
    return client_class.load_from_dict(config)


def build_click_conversion(client, row):
    """Map one row onto a ``ClickConversion`` message.

    The whole vendor mapping lives here: which field the click id goes in,
    the timestamp format, and the pair (value, currency) that Google treats
    as one — a value without a currency code is an upload it refuses.
    """
    conversion = client.get_type("ClickConversion")
    field = CLICK_ID_FIELDS.get(row.click_id_type)
    if field is None:
        raise ValueError(f"unknown click id type {row.click_id_type!r}")
    setattr(conversion, field, row.click_id)
    conversion.conversion_action = row.conversion_action
    conversion.conversion_date_time = google_datetime(row.conversion_at)
    if row.value is not None:
        conversion.conversion_value = float(Decimal(row.value))
        if row.currency:
            conversion.currency_code = row.currency
    return conversion


def send(row) -> tuple[str, str]:
    """Upload one row. Returns ``(status, reason)``; never touches the row.

    ``partial_failure`` is on with a single conversion in the request, so a
    validation refusal comes back as data (``partial_failure_error``)
    rather than as an SDK exception class this module would have to import
    to catch. Anything that is not a verdict — transport, auth, quota —
    leaves as :class:`GoogleAdsUnavailable`, because "Google said no" and
    "we could not ask" must not share a status.
    """
    client = build_client()
    try:
        service = client.get_service("ConversionUploadService")
        request = client.get_type("UploadClickConversionsRequest")
        request.customer_id = customer_id()
        request.conversions.append(build_click_conversion(client, row))
        request.partial_failure = True
        response = service.upload_click_conversions(request=request)
    except GoogleAdsUnavailable:
        raise
    except Exception as exc:  # the SDK's exception tree is not importable here
        raise GoogleAdsUnavailable(str(exc) or exc.__class__.__name__) from exc

    failure = getattr(response, "partial_failure_error", None)
    message = str(getattr(failure, "message", "") or "") if failure else ""
    if message:
        return STATUS_REJECTED, message
    results = list(getattr(response, "results", None) or [])
    if not results:
        # A response with neither a result nor an error is Google telling us
        # nothing. Calling that "uploaded" would put a fiction in the row.
        return STATUS_REJECTED, REASON_NO_RESULT
    return STATUS_UPLOADED, ""


# ── The outbox ───────────────────────────────────────────────────────


def enqueue(
    *,
    click_id,
    click_id_type="gclid",
    conversion_action="",
    conversion_at,
    clicked_at=None,
    value=None,
    currency="",
):
    """Write the conversion down. Idempotent on its natural key.

    Returns ``(row, created)``. A second call with the same
    ``(click_id, conversion_action, conversion_at)`` returns the FIRST row
    untouched — including its status — because that row may already have
    been uploaded, and overwriting it would re-open a settled fact.
    """
    from .models import ConversionUpload

    if click_id_type not in CLICK_ID_FIELDS:
        raise ValueError(f"unknown click id type {click_id_type!r}")
    return ConversionUpload.objects.get_or_create(
        click_id=str(click_id),
        conversion_action=str(conversion_action or ""),
        conversion_at=conversion_at,
        defaults={
            "click_id_type": click_id_type,
            "clicked_at": clicked_at,
            "value": value,
            "currency": str(currency or "")[:3].upper(),
        },
    )


def _backoff(attempts: int) -> timedelta:
    """Exponential, capped. The cap is what keeps a long outage bounded."""
    from .conf import analytics_settings

    base = int(analytics_settings.GOOGLE_ADS_RETRY_BASE_SECONDS)
    ceiling = int(analytics_settings.GOOGLE_ADS_RETRY_MAX_SECONDS)
    return timedelta(seconds=min(base * (2 ** max(attempts - 1, 0)), ceiling))


def _settle(row, status, reason=""):
    row.status = status
    row.reason = reason
    row.next_attempt_at = None
    row.save(update_fields=["status", "reason", "next_attempt_at", "updated_at"])
    return {"status": status, **({"reason": reason} if reason else {})}


def deliver(row) -> dict:
    """Attempt one row and record what happened. Returns the comm answer.

    Terminal rows answer from what they already know: a row that was
    uploaded is not uploaded again, which is the half of idempotence a
    unique constraint cannot provide.
    """
    from .conf import analytics_settings
    from .models import ConversionUpload

    if row.status != ConversionUpload.STATUS_PENDING:
        return {
            "status": row.status,
            **({"reason": row.reason} if row.reason else {}),
        }

    verdict = stale_verdict(row)
    if verdict:
        if verdict == REASON_EXPIRED:
            logger.info(
                "conversion upload %s settled as expired: click %s is past the "
                "%s-day window — it will not be retried",
                row.pk,
                row.clicked_at.isoformat() if row.clicked_at else "unknown",
                window().days,
            )
        return _settle(row, ConversionUpload.STATUS_SKIPPED, verdict)

    if not row.conversion_action:
        # Terminal: no configuration change supplies a conversion action
        # this row never carried. The caller has to enqueue it again with
        # one, which is a different conversion as far as the key goes.
        return _settle(
            row, ConversionUpload.STATUS_SKIPPED, REASON_NOT_CONFIGURED
        )

    if not is_configured():
        # NOT terminal, and the difference matters: credentials arriving
        # tomorrow should still upload today's conversions, which are well
        # inside the 90-day window. The row stays pending with the reason
        # visible; the ANSWER is `skipped`, because nothing was uploaded.
        row.reason = REASON_NOT_CONFIGURED
        row.next_attempt_at = timezone.now() + _backoff(1)
        row.save(update_fields=["reason", "next_attempt_at", "updated_at"])
        return {"status": STATUS_SKIPPED, "reason": REASON_NOT_CONFIGURED}

    row.attempts += 1
    try:
        status, reason = send(row)
    except GoogleAdsUnavailable as exc:
        reason = str(exc)
        limit = int(analytics_settings.GOOGLE_ADS_MAX_ATTEMPTS)
        if row.attempts >= limit:
            logger.error(
                "conversion upload %s gave up after %s attempts: %s",
                row.pk, row.attempts, reason,
            )
            row.save(update_fields=["attempts", "updated_at"])
            return _settle(
                row,
                ConversionUpload.STATUS_REJECTED,
                f"{REASON_MAX_ATTEMPTS}: {reason}",
            )
        row.reason = reason
        row.next_attempt_at = timezone.now() + _backoff(row.attempts)
        row.save(
            update_fields=["attempts", "reason", "next_attempt_at", "updated_at"]
        )
        logger.warning(
            "conversion upload %s deferred (attempt %s): %s",
            row.pk, row.attempts, reason,
        )
        return {"status": STATUS_PENDING, "reason": reason}

    row.save(update_fields=["attempts", "updated_at"])
    return _settle(row, status, reason)


def upload_click_conversion(payload: dict) -> dict:
    """Enqueue one conversion and try to deliver it. The comm entry point.

    Enqueue and delivery are deliberately NOT one transaction: the row must
    survive a delivery that raises, which is the entire reason it exists.
    """
    conversion_at = parse_timestamp(payload["conversion_at"])
    clicked_at = parse_timestamp(payload.get("clicked_at"))
    value = payload.get("value")
    with transaction.atomic():
        row, _created = enqueue(
            click_id=payload["click_id"],
            click_id_type=payload.get("click_id_type") or "gclid",
            conversion_action=payload.get("conversion_action") or "",
            conversion_at=conversion_at,
            clicked_at=clicked_at,
            value=Decimal(str(value)) if value is not None else None,
            currency=payload.get("currency") or "",
        )
    return deliver(row)


def due(limit: int = 100):
    """Pending rows whose backoff has elapsed, oldest first."""
    from django.db.models import Q

    from .models import ConversionUpload

    now = timezone.now()
    return list(
        ConversionUpload.objects.filter(status=ConversionUpload.STATUS_PENDING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("created_at")[: max(int(limit), 0)]
    )


__all__ = [
    "CLICK_ID_FIELDS",
    "GoogleAdsUnavailable",
    "REQUIRED_CREDENTIALS",
    "build_click_conversion",
    "build_client",
    "credentials",
    "deliver",
    "due",
    "enqueue",
    "expire_stale",
    "expiry_verdict",
    "google_datetime",
    "is_configured",
    "parse_timestamp",
    "send",
    "stale_verdict",
    "upload_click_conversion",
    "window_verdict",
]
