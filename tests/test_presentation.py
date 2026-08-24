"""Presenters, admin, and the small edges the API layer leaves.

Small surfaces, but each one is a place where "it renders" and "it renders
the right thing" are different questions.
"""
import pytest

from stapel_analytics import services
from stapel_analytics.admin import FunnelAdmin
from stapel_analytics.models import Funnel
from stapel_analytics.presenters import (
    get_funnel_presenter,
    get_receipt_presenter,
    get_registry_presenter,
    get_report_presenter,
    present_funnel_spec,
)

STEPS = ["a.b", "c.d"]


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {
        "EVENTS": {name: {"description": name} for name in STEPS}
    }


@pytest.mark.django_db
class TestFunnelPresenter:
    def test_it_presents_the_row(self):
        row = services.save_funnel(slug="c", steps=STEPS, title="C")
        dto = get_funnel_presenter()().present(row)
        assert dto.slug == "c" and dto.steps == STEPS and dto.title == "C"

    def test_the_id_is_a_string(self):
        row = services.save_funnel(slug="c", steps=STEPS)
        assert isinstance(get_funnel_presenter()().present(row).id, str)

    def test_a_db_funnel_reports_its_source(self):
        row = services.save_funnel(slug="c", steps=STEPS)
        assert get_funnel_presenter()().present(row).source == "db"

    def test_the_presenter_is_swappable(self, settings):
        settings.STAPEL_SWAP = {
            "ANALYTICS_FUNNEL_PRESENTER":
                "stapel_analytics.tests.test_presentation.NarrowFunnelPresenter"
        }
        # Compared by name: pytest's importlib mode gives the test module
        # a different identity from the one import_string resolves.
        assert get_funnel_presenter().__name__ == "NarrowFunnelPresenter"


from stapel_analytics.presenters import FunnelPresenter  # noqa: E402


class NarrowFunnelPresenter(FunnelPresenter):
    """A host override, used to prove the swap seam is live."""


class TestSpecPresentation:
    def test_a_declared_spec_presents(self, settings):
        from stapel_analytics.funnels import declared_funnels

        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"title": "Sell", "steps": STEPS,
                                 "window_seconds": 60}},
        }
        dto = present_funnel_spec(declared_funnels()["sell"])
        assert dto.source == "settings" and dto.window_seconds == 60
        assert dto.id is None


class TestReceiptPresenter:
    def test_it_builds_the_receipt(self):
        dto = get_receipt_presenter()().present(
            {"accepted": 2, "rejected": [{"index": 0}], "unregistered": ["x"],
             "source": "web"}
        )
        assert dto.accepted == 2 and dto.unregistered == ["x"]

    def test_missing_keys_default_safely(self):
        dto = get_receipt_presenter()().present({})
        assert dto.accepted == 0 and dto.rejected == [] and dto.source == ""


class TestRegistryPresenter:
    def test_it_lists_declared_and_builtin_events(self):
        dto = get_registry_presenter()().present()
        names = {entry.name for entry in dto.events}
        assert {"a.b", "flow.*"} <= names

    def test_the_builtin_flag_is_set(self):
        dto = get_registry_presenter()().present()
        flags = {entry.name: entry.builtin for entry in dto.events}
        assert flags["flow.*"] is True and flags["a.b"] is False

    def test_a_missing_flow_presents_as_none(self):
        dto = get_registry_presenter()().present()
        entries = {entry.name: entry for entry in dto.events}
        assert entries["a.b"].flow is None


@pytest.mark.django_db
class TestReportPresenter:
    def test_it_presents_a_funnel_report(self):
        from stapel_analytics.funnels import funnel_report

        services.save_funnel(slug="c", steps=STEPS)
        dto = get_report_presenter()().present(funnel_report("c"))
        assert [step.name for step in dto.steps] == STEPS
        assert dto.truncated is False

    def test_it_presents_an_events_report(self):
        from stapel_analytics.store import rollup

        services.track("a.b", {}, user_hash="u1")
        services.track("a.b", {}, user_hash="u2")
        services.track("c.d", {}, user_hash="u1")
        rows = rollup(group_by=["name"])
        dto = get_report_presenter()().present_events(
            rows, group_by=["name"], start=None, end=None
        )
        assert dto.total == 3
        assert dto.buckets[0].count == 2  # ordered by count, descending


@pytest.mark.django_db
class TestModelAndAdmin:
    def test_the_title_is_the_string_form(self):
        row = Funnel.objects.create(slug="c", steps=STEPS, title="Checkout")
        assert str(row) == "Checkout"

    def test_the_slug_is_the_fallback_string_form(self):
        row = Funnel.objects.create(slug="c", steps=STEPS)
        assert str(row) == "c"

    def test_the_admin_shows_a_step_count(self):
        row = Funnel.objects.create(slug="c", steps=STEPS)
        assert FunnelAdmin(Funnel, None).step_count(row) == 2

    def test_the_admin_handles_a_stepless_funnel(self):
        row = Funnel.objects.create(slug="c", steps=[])
        assert FunnelAdmin(Funnel, None).step_count(row) == 0

    def test_the_model_carries_an_access_declaration(self):
        from stapel_core.access.declaration import is_declared

        assert is_declared(Funnel)


class TestErrorKeysView:
    def test_it_serves_this_modules_keys(self):
        from stapel_analytics.errors import (
            STAPEL_ANALYTICS_ERRORS,
            AnalyticsErrorKeysView,
        )

        served = AnalyticsErrorKeysView().get_service_errors()
        assert set(STAPEL_ANALYTICS_ERRORS) <= set(served)


@pytest.mark.django_db
class TestViewEdges:
    def test_a_service_refusal_becomes_the_error_envelope(self, authed_client):
        """The decorator that maps AnalyticsError onto StapelErrorResponse."""
        response = authed_client.post(
            "/analytics/api/v1/funnels",
            {"slug": "c", "steps": ["a.b", "nope"]},
            format="json",
        )
        assert response.status_code == 400
        assert "localizable_error" in response.json()

    def test_an_unknown_funnel_in_a_report_becomes_404(self, authed_client, settings):
        """resolve_funnel raising UnknownFunnel inside the report view."""
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"steps": STEPS}},
        }
        # A declared funnel scopes as 409 but still reports; removing the
        # declaration mid-request is the path that reaches UnknownFunnel.
        response = authed_client.get("/analytics/api/v1/funnels/gone/report")
        assert response.status_code == 404

    def test_a_staff_caller_reaches_a_strangers_funnel(self, staff_client, other_user):
        services.save_funnel(slug="theirs", steps=STEPS, owner_id=other_user.pk)
        assert staff_client.get("/analytics/api/v1/funnels/theirs").status_code == 200
