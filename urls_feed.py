"""The conversion feed on its own — one route, nothing else.

``urls.py`` is the module's whole HTTP surface: a collector, funnels,
reports. A host that mounts this library only to hand an ad platform a file
wants none of that, and mounting it anyway would put an anonymous ingest
route on a service whose reason for installing analytics was an outbox
table. So the feed is separately mountable::

    path("billing/analytics/", include("stapel_analytics.urls_feed"))
    # -> /billing/analytics/api/v1/conversions/google-ads.csv

The route, its name and its view are the SAME objects ``urls_v1`` mounts —
``FEED_PATTERNS`` is imported, not restated — so the path a host reaches
through this door and the one it reaches through the full mount can never
be two different paths. Mounting both in one project is legal and pointless:
the second reverse of ``analytics-conversion-feed`` wins, and both resolve
to the same view.
"""
from django.urls import include, path

from stapel_analytics.urls_v1 import FEED_PATTERNS

urlpatterns = [
    path("api/v1/", include(list(FEED_PATTERNS))),
]

__all__ = ["urlpatterns"]
