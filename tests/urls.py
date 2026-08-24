from django.urls import include, path

urlpatterns = [
    path("analytics/", include("stapel_analytics.urls")),
]
