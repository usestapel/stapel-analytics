"""The HTTP surface: ingest, registry, funnel CRUD, reports."""
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.utils import timezone

from stapel_analytics import services
from stapel_analytics.models import Funnel

INGEST = "/analytics/api/v1/events"
REGISTRY = "/analytics/api/v1/event-registry"
FUNNELS = "/analytics/api/v1/funnels"
EVENTS_REPORT = "/analytics/api/v1/reports/events"
ERROR_KEYS = "/analytics/api/v1/error-keys/"

STEPS = ["step.one", "step.two"]


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {
        "EVENTS": {name: {"description": name, "flow": "f"} for name in STEPS},
    }


def _event(name=STEPS[0], **overrides):
    base = {
        "kind": "track",
        "name": name,
        "props": {},
        "ts": int(timezone.now().timestamp() * 1000),
    }
    base.update(overrides)
    return base


@pytest.mark.django_db
class TestIngestEndpoint:
    def test_a_batch_is_accepted_with_202(self, api_client):
        response = api_client.post(INGEST, {"events": [_event()]}, format="json")
        assert response.status_code == 202

    def test_the_receipt_names_what_happened(self, api_client):
        body = api_client.post(
            INGEST,
            {"events": [_event(), _event(name="nobody.declared")]},
            format="json",
        ).json()
        assert body["accepted"] == 2
        assert body["unregistered"] == ["nobody.declared"]

    def test_a_malformed_batch_is_400(self, api_client):
        response = api_client.post(INGEST, {"nope": []}, format="json")
        assert response.status_code == 400
        assert response.json()["localizable_error"] == "error.400.analytics_batch_shape"

    def test_a_non_object_body_is_400(self, api_client):
        response = api_client.post(INGEST, [1, 2], format="json")
        assert response.status_code == 400

    def test_an_oversized_batch_is_400_with_the_cap(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS, "MAX_BATCH_SIZE": 1}
        response = api_client.post(
            INGEST, {"events": [_event(), _event()]}, format="json"
        )
        assert response.status_code == 400
        assert response.json()["params"]["max"] == 1

    def test_an_oversized_body_is_413(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS, "MAX_BODY_BYTES": 10}
        response = api_client.post(INGEST, {"events": [_event()]}, format="json")
        assert response.status_code == 413

    def test_a_rejection_carries_its_reason(self, api_client):
        body = api_client.post(
            INGEST, {"events": [_event(props={"email": "a@b.com"})]}, format="json"
        ).json()
        assert body["accepted"] == 0
        assert body["rejected"][0]["reason"] == "pii"

    def test_an_empty_batch_is_accepted(self, api_client):
        assert api_client.post(INGEST, {"events": []}, format="json").json() == {
            "accepted": 0, "rejected": [], "unregistered": [], "source": "web"
        }

    def test_ingest_needs_no_authentication(self, api_client):
        assert api_client.post(
            INGEST, {"events": [_event()]}, format="json"
        ).status_code == 202

    def test_the_authenticator_seam_is_live(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "INGEST_AUTHENTICATION": [
                "rest_framework.authentication.TokenAuthentication"
            ],
        }
        from stapel_analytics.views import IngestView

        assert type(IngestView().get_authenticators()[0]).__name__ == (
            "TokenAuthentication"
        )


@pytest.mark.django_db
class TestEventRegistryEndpoint:
    def test_anonymous_callers_are_refused(self, api_client):
        assert api_client.get(REGISTRY).status_code in (401, 403)

    def test_the_anonymous_refusal_is_the_fleet_envelope(self, api_client):
        """The refusal body, not only its status code.

        No line of ``views.py`` builds this response: DRF's permission layer
        raises it, and the only seam that dresses it is
        ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``. This harness defined no
        ``REST_FRAMEWORK`` dict at all, so DRF's own handler answered
        ``{"detail": "..."}`` here — a shape a frontend reading
        ``localizable_error`` cannot translate — and the test above could not
        tell, because a status code is the same either way.
        ``stapel_core.error_envelope.W001`` reports the settings hole; this
        asserts the behaviour it costs.
        """
        response = api_client.get(REGISTRY)

        assert response.status_code in (401, 403), response.content
        assert "localizable_error" in response.data, response.data
        assert response.data["localizable_error"].startswith("error."), response.data
        assert set(response.data) >= {
            "localizable_error", "error", "params",
        }, response.data

    def test_an_authenticated_caller_reads_the_registry(self, authed_client):
        response = authed_client.get(REGISTRY)
        assert response.status_code == 200
        names = {entry["name"] for entry in response.json()["events"]}
        assert STEPS[0] in names

    def test_builtins_are_flagged(self, authed_client):
        entries = {e["name"]: e for e in authed_client.get(REGISTRY).json()["events"]}
        assert entries["flow.*"]["builtin"] is True
        assert entries[STEPS[0]]["builtin"] is False

    def test_the_mode_is_reported(self, authed_client):
        assert authed_client.get(REGISTRY).json()["mode"] == "warn"

    def test_the_flow_is_reported(self, authed_client):
        entries = {e["name"]: e for e in authed_client.get(REGISTRY).json()["events"]}
        assert entries[STEPS[0]]["flow"] == "f"

    def test_active_adapters_are_reported(self, authed_client, recording_adapter):
        assert "recorder" in authed_client.get(REGISTRY).json()["adapters"]

    def test_bridged_actions_are_reported(self, authed_client, settings):
        from stapel_analytics.actions import reset_comm_bridge, wire_comm_bridge

        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "COMM_BRIDGE": {"payment.completed": STEPS[1]},
        }
        wire_comm_bridge()
        try:
            body = authed_client.get(REGISTRY).json()
            assert body["bridged_actions"] == ["payment.completed"]
        finally:
            reset_comm_bridge()


@pytest.mark.django_db
class TestFunnelCrud:
    def test_creating_a_funnel_returns_201(self, authed_client):
        response = authed_client.post(
            FUNNELS, {"slug": "checkout", "steps": STEPS}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["slug"] == "checkout"

    def test_a_created_funnel_belongs_to_its_author(self, authed_client, user):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        assert Funnel.objects.get(slug="c").owner_id == user.pk

    def test_anonymous_callers_cannot_author(self, api_client):
        response = api_client.post(
            FUNNELS, {"slug": "c", "steps": STEPS}, format="json"
        )
        assert response.status_code in (401, 403)

    def test_a_duplicate_slug_is_refused(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        response = authed_client.post(
            FUNNELS, {"slug": "c", "steps": STEPS}, format="json"
        )
        assert response.status_code == 400
        assert response.json()["localizable_error"] == (
            "error.400.analytics_funnel_slug_taken"
        )

    def test_a_one_step_funnel_is_refused(self, authed_client):
        response = authed_client.post(
            FUNNELS, {"slug": "c", "steps": [STEPS[0]]}, format="json"
        )
        assert response.status_code == 400
        assert response.json()["localizable_error"] == (
            "error.400.analytics_funnel_steps"
        )

    def test_an_unregistered_step_is_refused(self, authed_client):
        response = authed_client.post(
            FUNNELS, {"slug": "c", "steps": [STEPS[0], "nobody.declared"]},
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["params"]["step"] == "nobody.declared"

    def test_the_per_owner_cap_is_enforced(self, authed_client, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS, "MAX_FUNNELS_PER_OWNER": 1
        }
        authed_client.post(FUNNELS, {"slug": "a", "steps": STEPS}, format="json")
        response = authed_client.post(
            FUNNELS, {"slug": "b", "steps": STEPS}, format="json"
        )
        assert response.status_code == 409

    def test_listing_shows_the_callers_funnels(self, authed_client, other_user):
        authed_client.post(FUNNELS, {"slug": "mine", "steps": STEPS}, format="json")
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        slugs = [row["slug"] for row in authed_client.get(FUNNELS).json()]
        assert slugs == ["mine"]

    def test_staff_see_every_funnel(self, staff_client, other_user):
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        slugs = [row["slug"] for row in staff_client.get(FUNNELS).json()]
        assert "theirs" in slugs

    def test_mine_narrows_a_staff_listing(self, staff_client, other_user):
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        slugs = [row["slug"] for row in staff_client.get(FUNNELS + "?mine=true").json()]
        assert slugs == []

    def test_reading_one_funnel(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        assert authed_client.get(f"{FUNNELS}/c").json()["steps"] == STEPS

    def test_a_missing_funnel_is_404(self, authed_client):
        response = authed_client.get(f"{FUNNELS}/nothing")
        assert response.status_code == 404
        assert response.json()["localizable_error"] == (
            "error.404.analytics_funnel_not_found"
        )

    def test_a_strangers_funnel_is_404_not_403(self, authed_client, other_user):
        """A slug is guessable; 'exists but not yours' is an oracle."""
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        assert authed_client.get(f"{FUNNELS}/theirs").status_code == 404

    def test_an_unowned_funnel_is_403(self, authed_client):
        """Created in code, or by an erased account: an operator's object."""
        services.save_funnel(slug="orphan", steps=STEPS)
        assert authed_client.get(f"{FUNNELS}/orphan").status_code == 403

    def test_patching_replaces_the_steps(self, authed_client, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "EVENTS": {
                **settings.STAPEL_ANALYTICS["EVENTS"],
                "step.three": {"description": "third"},
            },
        }
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        response = authed_client.patch(
            f"{FUNNELS}/c", {"steps": STEPS + ["step.three"]}, format="json"
        )
        assert response.json()["steps"] == STEPS + ["step.three"]

    def test_patching_revalidates_the_whole_rule(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        response = authed_client.patch(
            f"{FUNNELS}/c", {"steps": ["nobody.declared", STEPS[0]]}, format="json"
        )
        assert response.status_code == 400

    def test_patching_can_deactivate(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        response = authed_client.patch(f"{FUNNELS}/c", {"is_active": False},
                                       format="json")
        assert response.json()["is_active"] is False

    def test_deleting_removes_the_row(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        assert authed_client.delete(f"{FUNNELS}/c").status_code == 204
        assert not Funnel.objects.filter(slug="c").exists()

    def test_deleting_a_strangers_funnel_is_404(self, authed_client, other_user):
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        assert authed_client.delete(f"{FUNNELS}/theirs").status_code == 404


@pytest.mark.django_db
class TestDeclaredFunnelsOverHttp:
    @pytest.fixture(autouse=True)
    def _declare_one(self, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"title": "Sell", "steps": STEPS}},
        }

    def test_a_declared_funnel_is_listed(self, authed_client):
        rows = {row["slug"]: row for row in authed_client.get(FUNNELS).json()}
        assert rows["sell"]["source"] == "settings"

    def test_a_declared_funnel_is_readable(self, authed_client):
        assert authed_client.get(f"{FUNNELS}/sell").json()["title"] == "Sell"

    def test_authoring_over_a_declared_slug_is_refused(self, authed_client):
        response = authed_client.post(
            FUNNELS, {"slug": "sell", "steps": STEPS}, format="json"
        )
        assert response.status_code == 400

    def test_patching_a_declared_funnel_is_409(self, authed_client):
        response = authed_client.patch(f"{FUNNELS}/sell", {"steps": STEPS},
                                        format="json")
        assert response.status_code == 409
        assert response.json()["localizable_error"] == (
            "error.409.analytics_funnel_declared"
        )

    def test_deleting_a_declared_funnel_is_409(self, authed_client):
        assert authed_client.delete(f"{FUNNELS}/sell").status_code == 409

    def test_a_declared_funnel_reports(self, authed_client):
        assert authed_client.get(f"{FUNNELS}/sell/report").status_code == 200


@pytest.mark.django_db
class TestFunnelReportEndpoint:
    def test_a_report_is_returned(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        body = authed_client.get(f"{FUNNELS}/c/report").json()
        assert [step["name"] for step in body["steps"]] == STEPS

    def test_a_report_counts_events(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        base = timezone.now() - timedelta(minutes=10)
        services.track(STEPS[0], {}, user_hash="u1", ts=base)
        services.track(STEPS[1], {}, user_hash="u1", ts=base + timedelta(minutes=1))
        body = authed_client.get(f"{FUNNELS}/c/report").json()
        assert body["entered"] == 1 and body["completed"] == 1

    def test_compare_adds_previous_counts(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        body = authed_client.get(f"{FUNNELS}/c/report?compare=true").json()
        assert body["steps"][0]["previous_count"] == 0

    def test_an_explicit_period_is_honoured(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        start = quote((timezone.now() - timedelta(days=1)).isoformat())
        end = quote(timezone.now().isoformat())
        response = authed_client.get(f"{FUNNELS}/c/report?start={start}&end={end}")
        assert response.status_code == 200

    def test_an_empty_period_is_400(self, authed_client):
        authed_client.post(FUNNELS, {"slug": "c", "steps": STEPS}, format="json")
        moment = quote(timezone.now().isoformat())
        response = authed_client.get(f"{FUNNELS}/c/report?start={moment}&end={moment}")
        assert response.status_code == 400
        assert response.json()["localizable_error"] == (
            "error.400.analytics_report_period"
        )

    def test_reporting_an_unknown_funnel_is_404(self, authed_client):
        assert authed_client.get(f"{FUNNELS}/nothing/report").status_code == 404

    def test_a_strangers_funnel_report_is_404(self, authed_client, other_user):
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        assert authed_client.get(f"{FUNNELS}/theirs/report").status_code == 404


@pytest.mark.django_db
class TestEventsReportEndpoint:
    def test_staff_only(self, authed_client):
        assert authed_client.get(EVENTS_REPORT).status_code == 403

    def test_counts_group_by_name_by_default(self, staff_client):
        services.track(STEPS[0], {}, user_hash="u1")
        services.track(STEPS[0], {}, user_hash="u2")
        services.track(STEPS[1], {}, user_hash="u1")
        body = staff_client.get(EVENTS_REPORT).json()
        counts = {b["group"]["name"]: b["count"] for b in body["buckets"]}
        assert counts == {STEPS[0]: 2, STEPS[1]: 1}
        assert body["total"] == 3

    def test_grouping_by_source_is_accepted(self, staff_client):
        services.track(STEPS[0], {}, user_hash="u1", source="ios")
        body = staff_client.get(EVENTS_REPORT + "?group_by=source").json()
        assert body["buckets"][0]["group"] == {"source": "ios"}

    def test_an_unknown_group_by_is_refused(self, staff_client):
        assert staff_client.get(EVENTS_REPORT + "?group_by=secret").status_code == 400

    def test_an_empty_period_is_400(self, staff_client):
        moment = quote(timezone.now().isoformat())
        response = staff_client.get(f"{EVENTS_REPORT}?start={moment}&end={moment}")
        assert response.status_code == 400

    def test_buckets_are_ordered_by_count(self, staff_client):
        services.track(STEPS[0], {}, user_hash="u1")
        for index in range(3):
            services.track(STEPS[1], {}, user_hash=f"u{index}")
        body = staff_client.get(EVENTS_REPORT).json()
        assert body["buckets"][0]["group"]["name"] == STEPS[1]


@pytest.mark.django_db
class TestErrorKeys:
    def test_staff_read_the_listing(self, staff_client):
        response = staff_client.get(ERROR_KEYS)
        assert response.status_code == 200
        assert "error.404.analytics_funnel_not_found" in response.json()

    def test_the_per_event_keys_are_listed(self, staff_client):
        assert "error.400.analytics_event_pii" in staff_client.get(ERROR_KEYS).json()

    def test_a_plain_user_is_refused(self, authed_client):
        assert authed_client.get(ERROR_KEYS).status_code == 403


@pytest.mark.django_db
class TestSerializerSeam:
    def test_every_view_carries_the_mixin(self):
        from rest_framework.views import APIView

        from stapel_analytics import views

        seam_views = [
            value
            for value in vars(views).values()
            if isinstance(value, type)
            and issubclass(value, APIView)
            and value is not APIView
        ]
        assert seam_views
        for view in seam_views:
            assert issubclass(view, views.SerializerSeamMixin), view.__name__

    def test_a_host_can_swap_a_response_serializer(self, authed_client):
        from stapel_analytics.serializers import EventRegistrySerializer
        from stapel_analytics.views import EventRegistryView

        class Narrowed(EventRegistrySerializer):
            pass

        view = EventRegistryView()
        view.response_serializer_class = Narrowed
        assert view.get_response_serializer_class() is Narrowed
