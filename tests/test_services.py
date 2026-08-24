"""The domain: server-side track, funnel authoring, retention."""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_analytics import services
from stapel_analytics.models import Funnel
from stapel_analytics.privacy import PiiRefused, hash_user_id
from stapel_analytics.store import iter_events

DECLARED = {"EVENTS": {"a.b": {"description": "x"}, "c.d": {"description": "y"}}}


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = dict(DECLARED)


@pytest.mark.django_db
class TestTrack:
    def test_it_records_one_row(self):
        assert services.track("a.b", {"k": 1}) == {"accepted": 1,
                                                   "unregistered": False}
        assert len(list(iter_events())) == 1

    def test_the_props_are_stored(self):
        services.track("a.b", {"k": 1})
        assert list(iter_events())[0]["props"] == {"k": 1}

    def test_a_user_id_is_hashed(self):
        services.track("a.b", {}, user_id="u-1")
        assert list(iter_events())[0]["user_hash"] == hash_user_id("u-1")

    def test_a_user_hash_is_used_as_given(self):
        services.track("a.b", {}, user_hash="h")
        assert list(iter_events())[0]["user_hash"] == "h"

    def test_the_server_source_is_the_default(self):
        services.track("a.b", {})
        assert list(iter_events())[0]["source"] == "server"

    def test_an_explicit_source_wins(self):
        services.track("a.b", {}, source="worker")
        assert list(iter_events())[0]["source"] == "worker"

    def test_an_unregistered_event_is_marked(self):
        assert services.track("nobody.declared", {})["unregistered"] is True

    def test_reject_mode_refuses(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "REGISTRY_MODE": "reject"}
        with pytest.raises(services.AnalyticsError) as exc:
            services.track("nobody.declared", {})
        assert exc.value.status == 400

    def test_off_mode_marks_nothing(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "REGISTRY_MODE": "off"}
        assert services.track("nobody.declared", {})["unregistered"] is False

    def test_the_pii_guard_applies_to_server_events(self):
        """An app-layer module that could bypass it would be the hole."""
        with pytest.raises(PiiRefused):
            services.track("a.b", {"email": "a@b.com"})

    def test_an_explicit_timestamp_is_used(self):
        moment = timezone.now() - timedelta(days=1)
        services.track("a.b", {}, ts=moment)
        assert abs((list(iter_events())[0]["ts"] - moment).total_seconds()) < 1

    def test_a_page_kind_skips_the_registry(self):
        assert services.track("/some/path", {}, kind="page")["unregistered"] is False


@pytest.mark.django_db
class TestRecordBatch:
    def test_it_returns_a_receipt(self):
        now_ms = int(timezone.now().timestamp() * 1000)
        result = services.record_batch(
            {"events": [{"kind": "track", "name": "a.b", "ts": now_ms}]}
        )
        assert result["accepted"] == 1 and result["source"] == "web"

    def test_an_empty_batch_writes_nothing(self):
        assert services.record_batch({"events": []})["accepted"] == 0
        assert list(iter_events()) == []


@pytest.mark.django_db
class TestValidateSteps:
    def test_two_registered_steps_pass(self):
        assert services.validate_steps(["a.b", "c.d"]) == ["a.b", "c.d"]

    def test_whitespace_is_trimmed(self):
        assert services.validate_steps([" a.b ", "c.d"]) == ["a.b", "c.d"]

    def test_blank_steps_are_dropped(self):
        with pytest.raises(services.AnalyticsError):
            services.validate_steps(["a.b", "  "])

    def test_one_step_is_refused(self):
        with pytest.raises(services.AnalyticsError):
            services.validate_steps(["a.b"])

    def test_a_scalar_is_refused(self):
        with pytest.raises(services.AnalyticsError):
            services.validate_steps("a.b")

    def test_too_many_steps_are_refused(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "MAX_FUNNEL_STEPS": 2}
        with pytest.raises(services.AnalyticsError):
            services.validate_steps(["a.b", "c.d", "a.b"])

    def test_an_unregistered_step_is_refused(self):
        with pytest.raises(services.AnalyticsError) as exc:
            services.validate_steps(["a.b", "nobody"])
        assert exc.value.params["step"] == "nobody"

    def test_mode_off_accepts_anything(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "REGISTRY_MODE": "off"}
        assert services.validate_steps(["x", "y"]) == ["x", "y"]


@pytest.mark.django_db
class TestSaveFunnel:
    def test_it_creates_a_row(self):
        row = services.save_funnel(slug="c", steps=["a.b", "c.d"], title="C")
        assert Funnel.objects.get(pk=row.pk).title == "C"

    def test_the_default_window_is_applied(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED,
                                     "DEFAULT_FUNNEL_WINDOW_SECONDS": 42}
        row = services.save_funnel(slug="c", steps=["a.b", "c.d"])
        assert row.window_seconds == 42

    def test_an_explicit_window_wins(self):
        row = services.save_funnel(slug="c", steps=["a.b", "c.d"],
                                   window_seconds=99)
        assert row.window_seconds == 99

    def test_updating_an_instance_keeps_the_slug(self):
        row = services.save_funnel(slug="c", steps=["a.b", "c.d"])
        updated = services.save_funnel(slug="c", steps=["c.d", "a.b"],
                                        instance=row)
        assert updated.pk == row.pk and updated.steps == ["c.d", "a.b"]

    def test_deleting_removes_the_row(self):
        row = services.save_funnel(slug="c", steps=["a.b", "c.d"])
        services.delete_funnel(row)
        assert not Funnel.objects.exists()

    def test_the_cap_only_applies_to_owned_funnels(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "MAX_FUNNELS_PER_OWNER": 1}
        services.save_funnel(slug="a", steps=["a.b", "c.d"])
        # No owner: no cap to hit — this is an operator's object.
        services.save_funnel(slug="b", steps=["a.b", "c.d"])
        assert Funnel.objects.count() == 2


@pytest.mark.django_db
class TestRetention:
    def test_nothing_is_purged_without_a_horizon(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": None}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=1000))
        assert services.purge_events() == 0
        assert len(list(iter_events())) == 1

    def test_old_rows_go(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": 30}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=60))
        services.track("c.d", {}, ts=timezone.now())
        assert services.purge_events() == 1
        assert [row["name"] for row in iter_events()] == ["c.d"]

    def test_an_explicit_horizon_wins(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "RETENTION_DAYS": 3650}
        services.track("a.b", {}, ts=timezone.now() - timedelta(days=60))
        removed = services.purge_events(
            older_than=timezone.now() - timedelta(days=30)
        )
        assert removed == 1
