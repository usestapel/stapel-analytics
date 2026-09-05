"""Models of stapel-analytics — three tables, and the one it does NOT have.

The events themselves have **no model here**. They live in
``stapel_core.eventstore`` (``store.py``), the fleet's append-only stream
primitive, which is already the design's "partitionable table, retention is a
setting". Giving this module its own event table would mean
a second unbounded time-series table in every deployment, with its own
partitioning, its own retention sweep and its own scale-out story — three
problems core solved once.

What IS a row is the **funnel**: a named, ordered list of event names with a
conversion window. It is small, edited by hand, read by a dashboard, and
belongs to someone — everything an event row is not.

The second row is the **conversion upload**: one offline click conversion
on its way to an ad platform. It is a transactional outbox entry, not
analytics data — it exists because the delivery is a call to somebody
else's API, and a conversion lost to their outage is bidding signal the
advertiser never gets back.

The third is the **feed fetch**: one line saying somebody pulled the
conversion feed, when, and how many rows they got. It exists because the
feed inverts the delivery — the ad platform reads on its own schedule, and
nothing in a pull tells the server it happened. Without this row the only
answerable question is "is the endpoint up", and the question an operator
actually has is "has the first load happened yet", which no amount of
uptime answers.

A funnel may also arrive DECLARED, from ``STAPEL_ANALYTICS["FUNNELS"]``:
that is the Studio path (analytics-standard §4 — the CTO agent declares a
funnel together with the feature, in the project spec). Declared funnels are
read-only over the API and merge UNDER these rows, so a deployment can see
both without either pretending to be the other.

House rules (docs/library-standard.md §3.8): cross-service references are
UUID fields, not FKs; the user model only via ``settings.AUTH_USER_MODEL``;
index/constraint names <= 30 chars.
"""
import uuid

from django.db import models
from stapel_core.access import access


@access.standard  # a funnel names business milestones, never personal data
class Funnel(models.Model):
    """One conversion funnel: ordered event names plus a window.

    ``owner_id`` is an FK-less user id (the fleet's precedent for a row that
    must outlive an account by exactly as long as an operator needs to see
    it). ``workspace_id`` is the tenancy scope where the host has one; it is
    nullable because a single-tenant host has none and a mandatory column
    would be filled with a fiction.
    """

    #: Funnel identity. UUID, so an id may be handed to a client without
    #: leaking how many funnels the deployment holds.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: URL-safe identity. This is what a report is requested by, so it is
    #: unique deployment-wide rather than per owner: two funnels answering
    #: to "checkout" would make `/funnels/checkout/report` ambiguous, and a
    #: dashboard would show one tenant's numbers under another's name.
    slug = models.SlugField(max_length=100, unique=True)
    #: Human label for a dashboard. Blank falls back to the slug.
    title = models.CharField(max_length=200, blank=True, default="")
    #: What this funnel is for, in the words of whoever declared it.
    description = models.TextField(blank=True, default="")

    #: Ordered event names. Validated against the event registry at write
    #: time (services.save_funnel): a step nothing can ever emit is a funnel
    #: that reads 100% conversion to step 1 and 0% after it, forever.
    steps = models.JSONField(default=list)

    #: Conversion window in seconds, measured from a subject's FIRST step.
    window_seconds = models.PositiveIntegerField(default=604800)

    #: Inactive funnels stay readable and reportable but are excluded from
    #: the step-validity check — a funnel parked mid-redesign must not warn
    #: on every boot.
    is_active = models.BooleanField(default=True)

    #: FK-less user id of the author. Null = created in code, or the
    #: account was erased; such a funnel is an operator's object, not a
    #: user's, and the API answers 403 rather than 404 for it.
    owner_id = models.UUIDField(null=True, blank=True, db_index=True)
    #: Tenancy scope, where the host has one. Null in a single-tenant host.
    workspace_id = models.UUIDField(null=True, blank=True, db_index=True)

    #: When the funnel was authored.
    created_at = models.DateTimeField(auto_now_add=True)
    #: When it was last re-authored (a PATCH re-validates the whole rule).
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("slug",)
        indexes = [
            models.Index(fields=["workspace_id", "is_active"], name="anl_funnel_ws_idx"),
        ]

    def __str__(self):
        return self.title or self.slug




@access.sensitive  # a click id is an advertising identifier of one person
class ConversionUpload(models.Model):
    """One offline click conversion on its way to Google Ads.

    This is a **transactional outbox row**, not analytics data. It exists
    because the upload is a call to somebody else's API: it fails for
    reasons that have nothing to do with the conversion (an expired refresh
    token, a quota, a network), and a conversion lost to one of those is
    revenue the advertiser's bidding never learns about. So the fact is
    written down first, uploaded second, and retried by a command — the same
    shape ``analytics.events.recorded`` uses for fan-out, made durable
    because unlike a fan-out mirror there is no second copy to reconcile
    from.

    The row is its own idempotence key: ``(click_id, conversion_action,
    conversion_at)`` is unique, so enqueuing the same conversion twice —
    from a retried webhook, a replayed Action, an operator running the
    importer again — produces one row and at most one upload.

    House rules (docs/library-standard.md §3.8): index/constraint names
    <= 30 chars. No FK to the user: a click id is not an account, and the
    row must not pretend it knows whose it is.
    """

    #: What kind of click identifier ``click_id`` holds. Google takes each
    #: in its OWN request field — a gbraid put in ``gclid`` is not a
    #: mismatched value, it is a rejected upload.
    CLICK_ID_TYPES = (
        ("gclid", "gclid"),      # the classic per-click id
        ("gbraid", "gbraid"),    # app→web, iOS 14.5+ privacy-preserving
        ("wbraid", "wbraid"),    # web→app, iOS 14.5+ privacy-preserving
    )

    #: Outbox lifecycle. ``uploaded`` and ``rejected`` are terminal;
    #: ``skipped`` is terminal too but carries a reason that is about the
    #: DATA (outside the window) rather than about Google's verdict.
    STATUS_PENDING = "pending"
    STATUS_UPLOADED = "uploaded"
    STATUS_REJECTED = "rejected"
    STATUS_SKIPPED = "skipped"
    STATUSES = (
        (STATUS_PENDING, "pending"),
        (STATUS_UPLOADED, "uploaded"),
        (STATUS_REJECTED, "rejected"),
        (STATUS_SKIPPED, "skipped"),
    )

    #: The click identifier itself, opaque and long — Google does not
    #: document a maximum, and a truncated click id is a rejected upload.
    click_id = models.CharField(max_length=512)
    click_id_type = models.CharField(
        max_length=8, choices=CLICK_ID_TYPES, default="gclid"
    )

    #: Resource name of the conversion action the upload counts against
    #: (``customers/<cid>/conversionActions/<id>``). May be blank: a caller
    #: that does not know it yet still gets a durable row, and the upload
    #: comes back ``skipped`` with ``not_configured`` rather than being
    #: guessed at.
    conversion_action = models.CharField(max_length=255, blank=True, default="")

    #: When the conversion happened. Uploaded as Google's
    #: ``conversion_date_time``.
    conversion_at = models.DateTimeField()

    #: When the CLICK happened, if the caller knows. Optional because most
    #: callers do not have it, and load-bearing when they do: Google's
    #: 90-day window is measured from the click, and this is the only field
    #: from which that distance can actually be computed (see
    #: ``conversions.window_verdict``).
    clicked_at = models.DateTimeField(null=True, blank=True)

    #: Conversion value and its currency. Null value = a conversion that
    #: counts but carries no money, which is a real case (a lead), not a
    #: missing number to default to zero.
    value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=STATUSES, default=STATUS_PENDING, db_index=True
    )
    #: Why it was skipped or rejected, in the words of whoever decided —
    #: our own reason code for a skip, Google's message for a rejection.
    #: A status without one is a row an operator debugs by guessing.
    reason = models.TextField(blank=True, default="")

    #: Upload attempts made so far. Drives the backoff and the give-up.
    attempts = models.PositiveIntegerField(default=0)
    #: Earliest next attempt. Null = due now.
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["click_id", "conversion_action", "conversion_at"],
                name="anl_conv_upload_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"], name="anl_conv_due_idx"
            ),
        ]

    def __str__(self):
        return f"{self.click_id_type}:{self.click_id[:12]}… {self.status}"


class ConversionFeedFetch(models.Model):
    """One read of the conversion feed. The receipt a pull does not leave.

    Three columns and no more, because the interesting question is small:
    *when* was it read and *how much* was there. ``remote`` is the caller's
    address, kept short and best-effort — it is the difference between "the
    puller is configured" and "somebody found the URL", not an audit trail
    (there is no user here to audit; the caller is a token).

    Rows are trimmed by ``feed.record_fetch`` — the table answers a
    question about the recent past, and a receipt log that grows forever to
    answer it would be a second unbounded table in a module whose whole
    storage story is about not having one.
    """

    #: When the feed was served. Indexed because every read of this table
    #: is "the latest one" or "the ones since".
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: Rows the response carried. Zero is a real and important answer: the
    #: puller is working and the outbox is empty.
    rows = models.PositiveIntegerField(default=0)
    #: Best-effort remote address of the caller. Blank when the deployment
    #: sits behind a proxy that does not forward one — blank is honest,
    #: a guessed address is not.
    remote = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ("-at",)

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.rows} row(s)"


__all__ = ["ConversionFeedFetch", "ConversionUpload", "Funnel"]
