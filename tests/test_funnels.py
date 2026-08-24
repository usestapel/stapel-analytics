"""Funnels: definition merge, subject grouping, the window, comparison."""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_analytics import services
from stapel_analytics.funnels import (
    UnknownFunnel,
    declared_funnels,
    funnel_report,
    list_funnels,
    report,
    resolve_funnel,
)
from stapel_analytics.models import Funnel

STEPS = ["step.one", "step.two", "step.three"]


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {
        "EVENTS": {name: {"description": name} for name in STEPS},
    }


def emit(name, *, subject, at=None, kind="track", key="user_hash"):
    """Record one event for *subject* at *at*, straight through services."""
    services.track(
        name,
        {},
        kind=kind,
        ts=at or timezone.now(),
        **{key: subject},
    )


@pytest.mark.django_db
class TestDefinitionSources:
    def test_an_authored_funnel_resolves(self):
        services.save_funnel(slug="checkout", steps=STEPS[:2])
        assert resolve_funnel("checkout").source == "db"

    def test_a_declared_funnel_resolves(self, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"title": "Sell", "steps": STEPS[:2]}},
        }
        spec = resolve_funnel("sell")
        assert spec.source == "settings" and spec.title == "Sell"

    def test_an_unknown_slug_raises(self):
        with pytest.raises(UnknownFunnel):
            resolve_funnel("nothing")

    def test_a_declared_funnel_defaults_its_window(self, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "DEFAULT_FUNNEL_WINDOW_SECONDS": 111,
            "FUNNELS": {"sell": {"steps": STEPS[:2]}},
        }
        assert resolve_funnel("sell").window_seconds == 111

    def test_a_none_declared_funnel_is_skipped(self, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": None},
        }
        assert declared_funnels() == {}

    def test_a_scalar_declared_funnel_is_refused(self, settings):
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS, "FUNNELS": {"a": 1}}
        with pytest.raises(TypeError):
            declared_funnels()

    def test_a_scalar_funnels_map_is_refused(self, settings):
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS, "FUNNELS": ["a"]}
        with pytest.raises(TypeError):
            declared_funnels()


@pytest.mark.django_db
class TestListing:
    def test_declared_and_authored_are_listed_together(self, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"steps": STEPS[:2]}},
        }
        services.save_funnel(slug="checkout", steps=STEPS[:2])
        assert [spec.slug for spec in list_funnels()] == ["checkout", "sell"]

    def test_declared_funnels_can_be_excluded(self, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"steps": STEPS[:2]}},
        }
        assert list_funnels(include_declared=False) == []

    def test_owner_scoping_filters_rows(self, user, other_user):
        services.save_funnel(slug="mine", steps=STEPS[:2], owner_id=user.pk)
        services.save_funnel(slug="theirs", steps=STEPS[:2], owner_id=other_user.pk)
        assert [s.slug for s in list_funnels(owner_id=user.pk)] == ["mine"]

    def test_owner_scoping_does_not_hide_declared_funnels(self, settings, user):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "FUNNELS": {"sell": {"steps": STEPS[:2]}},
        }
        assert "sell" in [s.slug for s in list_funnels(owner_id=user.pk)]

    def test_workspace_scoping_filters_rows(self):
        import uuid

        workspace = uuid.uuid4()
        services.save_funnel(slug="a", steps=STEPS[:2], workspace_id=workspace)
        services.save_funnel(slug="b", steps=STEPS[:2])
        assert [s.slug for s in list_funnels(workspace_id=workspace)] == ["a"]


@pytest.mark.django_db
class TestConversion:
    def test_a_subject_passing_every_step_converts(self):
        services.save_funnel(slug="f", steps=STEPS)
        base = timezone.now() - timedelta(hours=1)
        for index, name in enumerate(STEPS):
            emit(name, subject="u1", at=base + timedelta(minutes=index))
        result = funnel_report("f")
        assert [step.count for step in result.steps] == [1, 1, 1]
        assert result.conversion == 1.0

    def test_a_subject_dropping_out_is_counted_up_to_the_drop(self):
        services.save_funnel(slug="f", steps=STEPS)
        base = timezone.now() - timedelta(hours=1)
        emit(STEPS[0], subject="u1", at=base)
        emit(STEPS[1], subject="u1", at=base + timedelta(minutes=1))
        result = funnel_report("f")
        assert [step.count for step in result.steps] == [1, 1, 0]
        assert result.steps[2].dropoff == 1

    def test_steps_out_of_order_do_not_convert(self):
        """Step two before step one is not a conversion, it is two events."""
        services.save_funnel(slug="f", steps=STEPS[:2])
        base = timezone.now() - timedelta(hours=1)
        emit(STEPS[1], subject="u1", at=base)
        emit(STEPS[0], subject="u1", at=base + timedelta(minutes=1))
        assert [s.count for s in funnel_report("f").steps] == [1, 0]

    def test_a_subject_who_never_entered_is_invisible(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        emit(STEPS[1], subject="u1")
        assert funnel_report("f").entered == 0

    def test_several_subjects_are_counted_independently(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        base = timezone.now() - timedelta(hours=1)
        for subject in ("u1", "u2", "u3"):
            emit(STEPS[0], subject=subject, at=base)
        emit(STEPS[1], subject="u1", at=base + timedelta(minutes=1))
        result = funnel_report("f")
        assert result.entered == 3 and result.completed == 1
        assert result.conversion == round(1 / 3, 6)

    def test_the_earliest_occurrence_of_a_step_is_used(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        base = timezone.now() - timedelta(hours=2)
        emit(STEPS[0], subject="u1", at=base + timedelta(minutes=30))
        emit(STEPS[0], subject="u1", at=base)
        emit(STEPS[1], subject="u1", at=base + timedelta(minutes=10))
        assert funnel_report("f").completed == 1

    def test_rates_are_relative_to_the_right_denominators(self):
        services.save_funnel(slug="f", steps=STEPS)
        base = timezone.now() - timedelta(hours=1)
        for subject in ("u1", "u2", "u3", "u4"):
            emit(STEPS[0], subject=subject, at=base)
        for subject in ("u1", "u2"):
            emit(STEPS[1], subject=subject, at=base + timedelta(minutes=1))
        emit(STEPS[2], subject="u1", at=base + timedelta(minutes=2))
        steps = funnel_report("f").steps
        assert steps[1].rate_from_first == 0.5
        assert steps[2].rate_from_first == 0.25
        assert steps[2].rate_from_previous == 0.5

    def test_the_first_step_rate_is_one_when_anybody_entered(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        emit(STEPS[0], subject="u1", at=timezone.now() - timedelta(minutes=1))
        assert funnel_report("f").steps[0].rate_from_previous == 1.0

    def test_an_empty_funnel_reports_zeros_without_dividing_by_zero(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        result = funnel_report("f")
        assert result.entered == 0 and result.conversion == 0.0
        assert result.steps[0].rate_from_previous == 0.0


@pytest.mark.django_db
class TestSubjects:
    def test_an_anonymous_visitor_is_a_subject(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        base = timezone.now() - timedelta(minutes=10)
        emit(STEPS[0], subject="anon-1", at=base, key="anon_id")
        emit(STEPS[1], subject="anon-1", at=base + timedelta(minutes=1),
             key="anon_id")
        assert funnel_report("f").completed == 1

    def test_a_session_is_a_subject_of_last_resort(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        base = timezone.now() - timedelta(minutes=10)
        emit(STEPS[0], subject="s-1", at=base, key="session_id")
        emit(STEPS[1], subject="s-1", at=base + timedelta(minutes=1),
             key="session_id")
        assert funnel_report("f").completed == 1

    def test_a_subjectless_event_is_not_a_conversion(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        services.track(STEPS[0], {})
        services.track(STEPS[1], {})
        assert funnel_report("f").entered == 0

    def test_the_subject_resolver_is_a_seam(self, settings):
        services.save_funnel(slug="f", steps=STEPS[:2])
        base = timezone.now() - timedelta(minutes=10)
        emit(STEPS[0], subject="a", at=base, key="anon_id")
        emit(STEPS[1], subject="b", at=base + timedelta(minutes=1), key="anon_id")
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "SUBJECT_RESOLVER": "stapel_analytics.tests.test_funnels.one_subject",
        }
        # With everybody the same person, the two events become one journey.
        assert funnel_report("f").completed == 1


def one_subject(row) -> str:
    """A resolver that says every event belongs to the same person."""
    return "everybody"


@pytest.mark.django_db
class TestWindow:
    def test_a_step_outside_the_window_does_not_convert(self):
        services.save_funnel(slug="f", steps=STEPS[:2], window_seconds=60)
        base = timezone.now() - timedelta(hours=2)
        emit(STEPS[0], subject="u1", at=base)
        emit(STEPS[1], subject="u1", at=base + timedelta(minutes=30))
        assert funnel_report("f").completed == 0

    def test_a_step_inside_the_window_converts(self):
        services.save_funnel(slug="f", steps=STEPS[:2], window_seconds=3600)
        base = timezone.now() - timedelta(hours=2)
        emit(STEPS[0], subject="u1", at=base)
        emit(STEPS[1], subject="u1", at=base + timedelta(minutes=30))
        assert funnel_report("f", start=base - timedelta(minutes=1)).completed == 1

    def test_the_window_is_measured_from_the_first_step(self):
        """Not from the previous step — the classic conversion window."""
        services.save_funnel(slug="f", steps=STEPS, window_seconds=1800)
        base = timezone.now() - timedelta(hours=3)
        emit(STEPS[0], subject="u1", at=base)
        emit(STEPS[1], subject="u1", at=base + timedelta(minutes=20))
        emit(STEPS[2], subject="u1", at=base + timedelta(minutes=40))
        result = funnel_report("f", start=base - timedelta(minutes=1))
        assert [step.count for step in result.steps] == [1, 1, 0]

    def test_a_zero_window_means_unbounded(self):
        row = Funnel.objects.create(slug="f", steps=STEPS[:2], window_seconds=0)
        base = timezone.now() - timedelta(days=10)
        emit(STEPS[0], subject="u1", at=base)
        emit(STEPS[1], subject="u1", at=timezone.now() - timedelta(minutes=1))
        result = report(resolve_funnel(row.slug), start=base - timedelta(days=1))
        assert result.completed == 1


@pytest.mark.django_db
class TestPeriodAndComparison:
    def test_events_outside_the_period_are_not_counted(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        emit(STEPS[0], subject="u1", at=timezone.now() - timedelta(days=3))
        result = funnel_report("f", start=timezone.now() - timedelta(hours=1))
        assert result.entered == 0

    def test_the_comparison_period_is_the_equal_length_one_before(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        end = timezone.now()
        start = end - timedelta(hours=1)
        emit(STEPS[0], subject="now", at=end - timedelta(minutes=10))
        emit(STEPS[0], subject="before", at=start - timedelta(minutes=10))
        result = funnel_report("f", start=start, end=end, compare=True)
        assert result.steps[0].count == 1
        assert result.steps[0].previous_count == 1
        assert result.steps[0].delta == 0
        assert result.compare_start == start - timedelta(hours=1)
        assert result.compare_end == start

    def test_delta_is_signed(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        end = timezone.now()
        start = end - timedelta(hours=1)
        for subject in ("a", "b"):
            emit(STEPS[0], subject=subject, at=end - timedelta(minutes=10))
        result = funnel_report("f", start=start, end=end, compare=True)
        assert result.steps[0].delta == 2

    def test_no_comparison_means_no_previous_counts(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        result = funnel_report("f")
        assert result.steps[0].previous_count is None
        assert result.steps[0].delta is None

    def test_an_empty_period_is_refused(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        now = timezone.now()
        with pytest.raises(ValueError):
            funnel_report("f", start=now, end=now)

    def test_the_default_period_is_the_funnel_window(self):
        services.save_funnel(slug="f", steps=STEPS[:2], window_seconds=3600)
        result = funnel_report("f")
        assert abs((result.end - result.start).total_seconds() - 3600) < 2


@pytest.mark.django_db
class TestBounds:
    def test_a_funnel_without_steps_is_refused(self):
        row = Funnel.objects.create(slug="empty", steps=[])
        with pytest.raises(ValueError):
            report(resolve_funnel(row.slug))

    def test_a_report_reports_truncation(self, settings):
        services.save_funnel(slug="f", steps=STEPS[:2])
        for index in range(5):
            emit(STEPS[0], subject=f"u{index}",
                 at=timezone.now() - timedelta(minutes=index + 1))
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS,
                                     "MAX_REPORT_EVENTS": 3}
        result = funnel_report("f")
        assert result.truncated is True
        assert result.scanned <= 3

    def test_an_untruncated_report_says_so(self):
        services.save_funnel(slug="f", steps=STEPS[:2])
        emit(STEPS[0], subject="u1", at=timezone.now() - timedelta(minutes=1))
        assert funnel_report("f").truncated is False
