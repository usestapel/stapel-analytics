"""The comm surface, under schema validation.

The suite runs with ``VALIDATE_SCHEMAS`` on, so every call here is also an
assertion that the committed contract in ``schemas/functions/`` matches the
payload the code actually accepts.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from stapel_core.comm import call

from stapel_analytics import services
from stapel_analytics.privacy import hash_user_id
from stapel_analytics.store import iter_events

DECLARED = {"EVENTS": {"a.b": {"description": "x", "flow": "f"},
                       "c.d": {"description": "y"}}}


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = dict(DECLARED)


@pytest.mark.django_db
class TestTrackFunction:
    def test_it_records(self):
        assert call("analytics.track", {"name": "a.b"}) == {
            "accepted": 1, "unregistered": False
        }

    def test_props_travel(self):
        call("analytics.track", {"name": "a.b", "props": {"k": 1}})
        assert list(iter_events())[0]["props"] == {"k": 1}

    def test_a_user_id_is_hashed(self):
        call("analytics.track", {"name": "a.b", "user_id": "u-1"})
        assert list(iter_events())[0]["user_hash"] == hash_user_id("u-1")

    def test_an_integer_user_id_is_accepted(self):
        call("analytics.track", {"name": "a.b", "user_id": 42})
        assert list(iter_events())[0]["user_hash"] == hash_user_id(42)

    def test_identity_fields_travel(self):
        call(
            "analytics.track",
            {"name": "a.b", "anon_id": "a-1", "session_id": "s-1",
             "source": "worker"},
        )
        row = list(iter_events())[0]
        assert (row["anon_id"], row["session_id"], row["source"]) == (
            "a-1", "s-1", "worker"
        )

    def test_an_unregistered_name_is_reported(self):
        assert call("analytics.track", {"name": "nobody"})["unregistered"] is True

    def test_the_pii_guard_applies(self):
        from stapel_analytics.privacy import PiiRefused

        with pytest.raises((PiiRefused, Exception)):
            call("analytics.track", {"name": "a.b", "props": {"e": "a@b.com"}})


class TestEventRegistryFunction:
    def test_it_lists_the_registry(self):
        body = call("analytics.event_registry", {})
        names = {entry["name"] for entry in body["events"]}
        assert "a.b" in names and "flow.*" in names

    def test_it_reports_the_mode(self):
        assert call("analytics.event_registry", {})["mode"] == "warn"

    def test_entries_carry_their_flow(self):
        entries = {e["name"]: e for e in call("analytics.event_registry", {})["events"]}
        assert entries["a.b"]["flow"] == "f"

    def test_entries_carry_their_props(self, settings):
        settings.STAPEL_ANALYTICS = {
            "EVENTS": {"a.b": {"description": "x",
                               "props": {"k": {"type": "string",
                                               "description": "d"}}}}
        }
        entries = {e["name"]: e for e in call("analytics.event_registry", {})["events"]}
        assert entries["a.b"]["props"]["k"]["type"] == "string"


@pytest.mark.django_db
class TestFunnelReportFunction:
    def test_it_computes(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        base = timezone.now() - timedelta(minutes=10)
        services.track("a.b", {}, user_hash="u1", ts=base)
        services.track("c.d", {}, user_hash="u1", ts=base + timedelta(minutes=1))
        body = call("analytics.funnel_report", {"slug": "c"})
        assert body["entered"] == 1 and body["completed"] == 1
        assert body["conversion"] == 1.0

    def test_the_steps_are_named(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        body = call("analytics.funnel_report", {"slug": "c"})
        assert [step["name"] for step in body["steps"]] == ["a.b", "c.d"]

    def test_an_explicit_period_is_parsed(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        body = call(
            "analytics.funnel_report",
            {
                "slug": "c",
                "start": (timezone.now() - timedelta(days=1)).isoformat(),
                "end": timezone.now().isoformat(),
            },
        )
        assert body["start"] is not None and body["end"] is not None

    def test_a_z_suffixed_period_is_parsed(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        stamp = (timezone.now() - timedelta(days=1)).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        assert call("analytics.funnel_report", {"slug": "c", "start": stamp})

    def test_compare_adds_previous_counts(self):
        services.save_funnel(slug="c", steps=["a.b", "c.d"])
        body = call("analytics.funnel_report", {"slug": "c", "compare": True})
        assert body["steps"][0]["previous_count"] == 0

    def test_an_unknown_funnel_raises(self):
        from stapel_analytics.funnels import UnknownFunnel

        with pytest.raises((UnknownFunnel, Exception)):
            call("analytics.funnel_report", {"slug": "nothing"})

    def test_a_declared_funnel_reports(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED,
                                     "FUNNELS": {"sell": {"steps": ["a.b", "c.d"]}}}
        assert call("analytics.funnel_report", {"slug": "sell"})["slug"] == "sell"


class TestSchemaRegistration:
    def test_every_function_has_a_committed_schema(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        files = {p.stem for p in (root / "schemas" / "functions").glob("*.json")}
        assert files == {
            "analytics.track",
            "analytics.event_registry",
            "analytics.funnel_report",
        }

    def test_the_emitted_topic_has_a_committed_schema(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        files = {p.stem for p in (root / "schemas" / "emits").glob("*.json")}
        assert "analytics.events.recorded" in files

    def test_an_undeclared_field_is_refused_by_the_schema(self):
        """`additionalProperties: false` is the contract, not decoration."""
        with pytest.raises(Exception):
            call("analytics.track", {"name": "a.b", "nonsense": 1})
