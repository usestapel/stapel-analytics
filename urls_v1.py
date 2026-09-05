"""v1 URL set — paths here are relative to the ``api/v1/`` mount contributed
by the root ``urls.py`` (api-versioning.md §2).

One route is anonymous (``events``) and the rest are not, which is the
module's whole access story in one file: sending an event is something a
visitor does, and reading what everybody sent is not.
"""
from typing import NamedTuple

from django.urls import path

from .errors import AnalyticsErrorKeysView
from .views import (
    ConversionFeedView,
    EventRegistryView,
    EventsReportView,
    FunnelDetailView,
    FunnelListCreateView,
    FunnelReportView,
    IngestView,
)

#: The conversion feed, alone. Its own tuple because it is the one block a
#: host may want WITHOUT the rest: a deployment that mounts this module only
#: to hand an ad platform a file has no collector, no funnels and no reports
#: to expose, and mounting them anyway would put an anonymous ingest route
#: on a service that never wanted one. ``urls_feed.py`` is that mount.
FEED_PATTERNS = (
    path(
        "conversions/google-ads.csv",
        ConversionFeedView.as_view(),
        name="analytics-conversion-feed",
    ),
)

urlpatterns = [
    # The collector. Anonymous by design — see views.IngestView.
    path("events", IngestView.as_view(), name="analytics-ingest"),
    # The vocabulary a client fires against.
    path("event-registry", EventRegistryView.as_view(), name="analytics-event-registry"),
    path("funnels", FunnelListCreateView.as_view(), name="analytics-funnels"),
    path("funnels/<slug:slug>", FunnelDetailView.as_view(), name="analytics-funnel"),
    path(
        "funnels/<slug:slug>/report",
        FunnelReportView.as_view(),
        name="analytics-funnel-report",
    ),
    path("reports/events", EventsReportView.as_view(), name="analytics-events-report"),
    # The listing the stapel-translate error collector reads.
    path("error-keys/", AnalyticsErrorKeysView.as_view(), name="analytics-error-keys"),
    *FEED_PATTERNS,
]


class GateEntry(NamedTuple):
    """One gated URL block (capability-config.md §2 p.2). ``flags`` compose
    with OR; empty flags = always on."""

    name: str
    flags: tuple
    patterns: tuple


#: The ingest route is its own gate: a deployment that collects nothing (an
#: on-prem install with analytics off) drops it and keeps the funnels its
#: server-side ``track()`` calls still fill. The rest has no per-method gate
#: — closing a fan-out adapter is a registry decision, not a route that
#: disappears — and is declared as one entry so the capabilities.json
#: emitter has a uniform mechanism.
GATE_REGISTRY: dict = {
    "analytics.ingest": GateEntry("analytics.ingest", (), (urlpatterns[0],)),
    "analytics.api": GateEntry(
        "analytics.api", (), tuple(urlpatterns[1:-len(FEED_PATTERNS)])
    ),
    # Its own gate, and off unless a token is configured: the feed is the
    # only route here that answers a caller holding nothing but a secret.
    "analytics.conversion_feed": GateEntry(
        "analytics.conversion_feed", (), FEED_PATTERNS
    ),
}
