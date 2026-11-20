"""Canonical-prefix URLconf for contract emission.

The host mounts analytics at ``path("analytics/", include("stapel_analytics.urls"))``,
which yields ``/analytics/api/v1/...``; this URLconf reproduces that mount so
drf-spectacular emits the same paths and ``analytics_api_v1_*`` operationIds.
"""
from django.urls import include, path

urlpatterns = [
    path("analytics/", include("stapel_analytics.urls")),
]
