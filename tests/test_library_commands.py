"""Management commands and the scheduled task.

Each command answers a question an operator asks from a container with no
HTTP exposure, which is where "why is this funnel flat" gets debugged.
"""
import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from stapel_analytics import services
from stapel_analytics.store import iter_events

DECLARED = {"EVENTS": {"a.b": {"description": "x", "flow": "f"},
                       "c.d": {"description": "y"}}}


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = dict(DECLARED)


def run(command, *args, **options):
    out = StringIO()
    call_command(command, *args, stdout=out, **options)
    return out.getvalue()


class TestEventRegistryCommand:
    def test_it_lists_the_events(self):
        assert "a.b" in run("analytics_event_registry")

    def test_builtins_are_marked(self):
        assert "* flow.*" in run("analytics_event_registry")

    def test_the_mode_is_printed(self):
        assert "mode=warn" in run("analytics_event_registry")

    def test_json_output_is_machine_readable(self):
        body = json.loads(run("analytics_event_registry", "--json"))
        assert body["mode"] == "warn"
        assert any(entry["event"] == "a.b" for entry in body["events"])

    def test_json_output_marks_builtins(self):
        body = json.loads(run("analytics_event_registry", "--json"))
        entries = {entry["event"]: entry for entry in body["events"]}
        assert entries["flow.*"]["builtin"] is True

    def test_an_emptied_registry_says_so(self, settings):
        settings.STAPEL_ANALYTICS = {
            "EVENTS": {name: None for name in ("flow.*", "identify")}
        }
        assert "empty" in run("analytics_event_registry")


@pytest.mark.django_db
class TestFunnelReportCommand:
    def test_it_prints_a_report(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        base = timezone.now() - timedelta(minutes=10)
        services.track("a.b", {}, user_hash="u1", ts=base)
        services.track("c.d", {}, user_hash="u1", ts=base + timedelta(minutes=1))
        output = run("analytics_funnel_report", "c")
        assert "conversion 100.00%" in output
        assert "a.b" in output

    def test_json_output_is_machine_readable(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        body = json.loads(run("analytics_funnel_report", "c", "--json"))
        assert body["slug"] == "c" and len(body["steps"]) == 2

    def test_compare_is_reported(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        base = timezone.now() - timedelta(minutes=10)
        services.track("a.b", {}, user_hash="u1", ts=base)
        assert "vs previous" in run("analytics_funnel_report", "c", "--compare")

    def test_an_unknown_funnel_is_a_command_error(self):
        with pytest.raises(CommandError):
            run("analytics_funnel_report", "nothing")

    def test_an_empty_period_is_a_command_error(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        moment = timezone.now().isoformat()
        with pytest.raises(CommandError):
            run("analytics_funnel_report", "c", "--start", moment, "--end", moment)

    def test_an_explicit_period_is_honoured(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        start = (timezone.now() - timedelta(days=2)).isoformat()
        assert "conversion" in run("analytics_funnel_report", "c", "--start", start)

    def test_truncation_is_announced(self, settings):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        for index in range(4):
            services.track("a.b", {}, user_hash=f"u{index}",
                           ts=timezone.now() - timedelta(minutes=index + 1))
        settings.STAPEL_ANALYTICS = {**DECLARED, "MAX_REPORT_EVENTS": 2}
        assert "TRUNCATED" in run("analytics_funnel_report", "c")


@pytest.mark.django_db
class TestFanoutCommand:
    def test_it_redelivers_a_range(self, recording_adapter):
        services.track("a.b", {}, ts=timezone.now() - timedelta(minutes=5))
        recording_adapter.clear()
        output = run("analytics_fanout")
        assert "1 event(s) re-delivered" in output
        assert len(recording_adapter) == 1

    def test_it_names_each_adapter(self, recording_adapter):
        services.track("a.b", {}, ts=timezone.now() - timedelta(minutes=5))
        assert "recorder" in run("analytics_fanout")

    def test_an_empty_range_says_so(self, recording_adapter):
        assert "no events in range" in run("analytics_fanout")

    def test_the_range_is_honoured(self, recording_adapter):
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=5))
        recording_adapter.clear()
        assert "no events in range" in run("analytics_fanout")
        since = (timezone.now() - timedelta(days=10)).isoformat()
        assert "1 event(s)" in run("analytics_fanout", "--since", since)

    def test_a_reversed_range_is_a_command_error(self):
        until = (timezone.now() - timedelta(days=1)).isoformat()
        since = timezone.now().isoformat()
        with pytest.raises(CommandError):
            run("analytics_fanout", "--since", since, "--until", until)

    def test_an_unknown_adapter_is_a_command_error(self, recording_adapter):
        services.track("a.b", {}, ts=timezone.now() - timedelta(minutes=5))
        with pytest.raises(CommandError):
            run("analytics_fanout", "--adapter", "nope")

    def test_only_narrows_to_one_adapter(self, recording_adapter):
        from stapel_analytics.adapters import register_adapter

        register_adapter("second", {"handler": lambda e, c: None, "enabled": True})
        services.track("a.b", {}, ts=timezone.now() - timedelta(minutes=5))
        recording_adapter.clear()
        output = run("analytics_fanout", "--adapter", "recorder")
        assert "recorder" in output and "second" not in output

    def test_batches_are_chunked(self, recording_adapter):
        base = timezone.now() - timedelta(minutes=30)
        for index in range(5):
            services.track("a.b", {}, ts=base + timedelta(seconds=index))
        recording_adapter.clear()
        run("analytics_fanout", "--batch-size", "2")
        assert [len(call["events"]) for call in recording_adapter] == [2, 2, 1]

    def test_a_failing_adapter_is_reported_not_raised(self):
        from stapel_analytics.adapters import register_adapter

        def explode(events, config):
            raise RuntimeError("vendor is down")

        register_adapter("broken", {"handler": explode, "enabled": True})
        services.track("a.b", {}, ts=timezone.now() - timedelta(minutes=5))
        assert "ERROR" in run("analytics_fanout")


@pytest.mark.django_db
class TestPurgeCommand:
    def test_it_purges_past_the_horizon(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": 30}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=60))
        services.track("c.d", {})
        assert "1 analytics event(s)" in run("purge_analytics")
        assert [row["name"] for row in iter_events()] == ["c.d"]

    def test_no_horizon_is_a_loud_no_op(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": None}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=600))
        assert "nothing purged" in run("purge_analytics")
        assert len(list(iter_events())) == 1

    def test_days_can_be_overridden(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": None}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=60))
        assert "1 analytics event(s)" in run("purge_analytics", "--days", "30")


@pytest.mark.django_db
class TestTasks:
    def test_the_purge_task_returns_counts(self, settings):
        from stapel_analytics.tasks import purge_analytics_events

        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": 30}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=60))
        assert purge_analytics_events() == {"removed": 1}

    def test_the_purge_task_is_a_no_op_without_a_horizon(self, settings):
        from stapel_analytics.tasks import purge_analytics_events

        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": None}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=600))
        assert purge_analytics_events() == {"removed": 0}

    def test_the_beat_schedule_names_the_stable_task_name(self):
        pytest.importorskip("celery")
        from stapel_analytics.tasks import PURGE_TASK_NAME, get_analytics_beat_schedule

        schedule = get_analytics_beat_schedule()
        assert schedule["analytics-purge"]["task"] == PURGE_TASK_NAME

    def test_the_beat_schedule_reads_the_configured_cadence(self, settings):
        pytest.importorskip("celery")
        from stapel_analytics.tasks import get_analytics_beat_schedule

        settings.STAPEL_ANALYTICS = {**DECLARED, "PURGE_SCHEDULE": {"hour": 1}}
        assert get_analytics_beat_schedule()["analytics-purge"]["schedule"]
