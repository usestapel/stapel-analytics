"""Models of stapel-analytics — one table, and the one it does NOT have.

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


__all__ = ["Funnel"]
