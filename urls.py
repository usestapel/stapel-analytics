"""Root URLconf for stapel-analytics — v1 canon mount (api-versioning.md §2).

Canon: ``/<mod>/api/v1/...`` — the version segment sits right after ``api/``.
The host project mounts this module root::

    path("analytics/", include("stapel_analytics.urls"))   # -> /analytics/api/v1/...

**One documented exception.** ``@stapel/analytics`` shipped BEFORE this
module existed (analytics-standard v1 was the frontend half), and its
``stapelCollectorProvider`` hardcodes ``COLLECTOR_PATH = "/analytics/api/
events"`` — an un-versioned path the canon does not have. Refusing it would
mean every already-deployed frontend silently 404s its batches; adding a
``v1`` to the facade would break every deployment whose backend is not
upgraded on the same day. So the alias is mounted next to the canon route,
carries the SAME view, and is switchable::

    STAPEL_ANALYTICS = {"LEGACY_INGEST_ALIAS": False}

The alias is a compatibility surface with an end date, not a second API: it
goes when the frontend facade targets ``api/v1`` (CHANGELOG, MODULE.md §10).
It is resolved at import time because a URLconf is built once; a host
flipping it needs a reload, which is what a URL change is anyway.
"""
from django.urls import include, path

from stapel_analytics.urls_v1 import GATE_REGISTRY  # noqa: F401  (re-export)

urlpatterns = [
    path("api/v1/", include("stapel_analytics.urls_v1")),
]


def _legacy_alias_enabled() -> bool:
    from .conf import analytics_settings

    return bool(analytics_settings.LEGACY_INGEST_ALIAS)


if _legacy_alias_enabled():
    from .views import IngestView

    urlpatterns.append(
        path("api/events", IngestView.as_view(), name="analytics-ingest-legacy")
    )
