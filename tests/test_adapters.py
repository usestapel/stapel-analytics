"""The fan-out adapter registry, the outbox path, and the built-ins."""
import pytest

from stapel_analytics import services
from stapel_analytics.adapters import (
    BUILTIN_ADAPTERS,
    AdapterError,
    UnknownAdapter,
    active_adapters,
    adapter_handler,
    deliver_log,
    deliver_webhook,
    fan_out,
    get_adapters,
    register_adapter,
    resolve_adapter,
)

DECLARED = {"EVENTS": {"a.b": {"description": "x"}}}


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = dict(DECLARED)


class TestRegistryMerge:
    def test_the_builtins_are_present_and_disabled(self):
        adapters = get_adapters()
        assert set(BUILTIN_ADAPTERS) <= set(adapters)
        assert not any(spec.get("enabled") for spec in adapters.values())

    def test_nothing_is_active_out_of_the_box(self):
        assert active_adapters() == {}

    def test_settings_enable_a_builtin_without_restating_it(self, settings):
        settings.STAPEL_ANALYTICS = {
            **DECLARED,
            "ADAPTERS": {"webhook": {"enabled": True,
                                     "config": {"url": "https://x.example"}}},
        }
        spec = resolve_adapter("webhook")
        assert spec["handler"] == "stapel_analytics.adapters.deliver_webhook"
        assert spec["config"]["url"] == "https://x.example"

    def test_config_merges_one_level_deep(self, settings):
        settings.STAPEL_ANALYTICS = {
            **DECLARED,
            "ADAPTERS": {"webhook": {"config": {"url": "https://x.example"}}},
        }
        # timeout came from the built-in and survives the overlay.
        assert resolve_adapter("webhook")["config"]["timeout"] == 10.0

    def test_none_removes_a_builtin(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "ADAPTERS": {"webhook": None}}
        assert "webhook" not in get_adapters()

    def test_a_scalar_spec_is_refused(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "ADAPTERS": {"x": "handler"}}
        with pytest.raises(TypeError):
            get_adapters()

    def test_a_new_adapter_can_be_declared(self, settings):
        settings.STAPEL_ANALYTICS = {
            **DECLARED,
            "ADAPTERS": {"posthog": {"handler": "app.x", "enabled": True}},
        }
        assert "posthog" in active_adapters()

    def test_runtime_registration_wins(self, settings):
        settings.STAPEL_ANALYTICS = {
            **DECLARED, "ADAPTERS": {"x": {"handler": "a", "enabled": True}}
        }
        register_adapter("x", {"handler": "b", "enabled": True})
        assert resolve_adapter("x")["handler"] == "b"

    def test_runtime_none_removes(self):
        register_adapter("webhook", None)
        assert "webhook" not in get_adapters()

    def test_an_unknown_adapter_raises(self):
        with pytest.raises(UnknownAdapter):
            resolve_adapter("nope")

    def test_an_adapter_defaults_to_enabled_when_it_says_nothing(self):
        register_adapter("x", {"handler": lambda events, config: None})
        assert "x" in active_adapters()

    def test_the_builtin_map_is_not_mutated_by_a_merge(self, settings):
        settings.STAPEL_ANALYTICS = {
            **DECLARED, "ADAPTERS": {"webhook": {"config": {"url": "https://x"}}}
        }
        get_adapters()
        assert BUILTIN_ADAPTERS["webhook"]["config"]["url"] == ""


class TestHandlerResolution:
    def test_a_callable_handler_is_returned_as_is(self):
        def handler(events, config):
            pass

        register_adapter("x", {"handler": handler, "enabled": True})
        assert adapter_handler("x") is handler

    def test_a_dotted_path_is_imported(self):
        register_adapter(
            "x", {"handler": "stapel_analytics.adapters.deliver_log", "enabled": True}
        )
        assert adapter_handler("x") is deliver_log

    def test_a_handlerless_adapter_raises(self):
        register_adapter("x", {"enabled": True})
        with pytest.raises(UnknownAdapter):
            adapter_handler("x")


class TestFanOut:
    def test_every_active_adapter_receives_the_batch(self, recording_adapter):
        events = [{"name": "a.b", "kind": "track"}]
        assert fan_out(events) == {"recorder": {"delivered": 1}}
        assert recording_adapter[0]["events"] == events

    def test_the_config_reaches_the_handler(self):
        seen = {}

        def handler(events, config):
            seen.update(config)

        register_adapter("x", {"handler": handler, "enabled": True,
                               "config": {"k": "v"}})
        fan_out([{"name": "a.b"}])
        assert seen == {"k": "v"}

    def test_a_disabled_adapter_is_skipped(self, recording_adapter):
        register_adapter("recorder", {"handler": lambda e, c: None, "enabled": False})
        assert fan_out([{"name": "a.b"}]) == {}

    def test_a_failing_adapter_is_contained(self, recording_adapter):
        def explode(events, config):
            raise RuntimeError("vendor is down")

        register_adapter("broken", {"handler": explode, "enabled": True})
        results = fan_out([{"name": "a.b"}])
        assert results["broken"]["delivered"] == 0
        assert "vendor is down" in results["broken"]["error"]
        # The working adapter still got its batch.
        assert results["recorder"]["delivered"] == 1

    def test_an_unresolvable_handler_is_contained(self):
        register_adapter("broken", {"handler": "no.such.module", "enabled": True})
        results = fan_out([{"name": "a.b"}])
        assert results["broken"]["delivered"] == 0

    def test_only_narrows_to_one_adapter(self, recording_adapter):
        register_adapter("second", {"handler": lambda e, c: None, "enabled": True})
        assert set(fan_out([{"name": "a.b"}], only="recorder")) == {"recorder"}

    def test_only_with_an_unknown_name_raises(self, recording_adapter):
        with pytest.raises(UnknownAdapter):
            fan_out([{"name": "a.b"}], only="nope")


@pytest.mark.django_db
class TestOutboxPath:
    def test_ingest_fans_out_through_the_action(self, api_client, recording_adapter):
        """Fan-out is driven by analytics.events.recorded, not by the view."""
        from django.utils import timezone

        api_client.post(
            "/analytics/api/v1/events",
            {"events": [{"kind": "track", "name": "a.b",
                         "ts": int(timezone.now().timestamp() * 1000)}]},
            format="json",
        )
        assert len(recording_adapter) == 1
        assert recording_adapter[0]["events"][0]["name"] == "a.b"

    def test_a_server_track_fans_out_too(self, recording_adapter):
        services.track("a.b", {"k": "v"})
        assert recording_adapter[0]["events"][0]["props"] == {"k": "v"}

    def test_the_announcement_carries_an_iso_timestamp(self, recording_adapter):
        services.track("a.b", {})
        assert isinstance(recording_adapter[0]["events"][0]["ts"], str)

    def test_fanout_can_be_switched_off(self, settings, recording_adapter):
        settings.STAPEL_ANALYTICS = {**DECLARED, "FANOUT_ENABLED": False}
        services.track("a.b", {})
        assert recording_adapter == []

    def test_the_store_still_records_when_fanout_is_off(self, settings):
        from stapel_analytics.store import iter_events

        settings.STAPEL_ANALYTICS = {**DECLARED, "FANOUT_ENABLED": False}
        services.track("a.b", {})
        assert len(list(iter_events())) == 1

    def test_a_large_batch_is_chunked(self, settings, recording_adapter):
        from django.utils import timezone

        settings.STAPEL_ANALYTICS = {**DECLARED, "FANOUT_BATCH_SIZE": 2}
        now_ms = int(timezone.now().timestamp() * 1000)
        events = [{"kind": "track", "name": "a.b", "ts": now_ms} for _ in range(5)]
        services.record_batch({"events": events})
        assert [len(call["events"]) for call in recording_adapter] == [2, 2, 1]

    def test_a_broken_adapter_does_not_break_ingest(self, api_client):
        from django.utils import timezone

        def explode(events, config):
            raise RuntimeError("down")

        register_adapter("broken", {"handler": explode, "enabled": True})
        response = api_client.post(
            "/analytics/api/v1/events",
            {"events": [{"kind": "track", "name": "a.b",
                         "ts": int(timezone.now().timestamp() * 1000)}]},
            format="json",
        )
        assert response.status_code == 202


class TestBuiltinLog:
    def test_it_logs_each_event(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="stapel_analytics.adapters"):
            deliver_log([{"kind": "track", "name": "a.b", "props": {}}], {})
        assert "a.b" in caplog.text

    def test_the_level_is_configurable(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="stapel_analytics.adapters"):
            deliver_log([{"kind": "track", "name": "a.b", "props": {}}],
                        {"level": "DEBUG"})
        assert "a.b" in caplog.text

    def test_an_unknown_level_falls_back_to_info(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="stapel_analytics.adapters"):
            deliver_log([{"kind": "track", "name": "a.b", "props": {}}],
                        {"level": "NONSENSE"})
        assert "a.b" in caplog.text


class TestBuiltinWebhook:
    def test_it_refuses_to_run_without_a_url(self):
        with pytest.raises(AdapterError):
            deliver_webhook([{"name": "a.b"}], {})

    def test_it_posts_the_batch(self, monkeypatch):
        import json

        sent = {}

        def fake_post(url, body, headers, timeout=10.0):
            sent.update(url=url, body=json.loads(body), headers=headers,
                        timeout=timeout)
            return 200

        monkeypatch.setattr("stapel_analytics.transport.post_json", fake_post)
        deliver_webhook(
            [{"name": "a.b"}],
            {"url": "https://collector.example/e", "headers": {"X-Key": "k"},
             "timeout": 3.0},
        )
        assert sent["url"] == "https://collector.example/e"
        assert sent["body"] == {"events": [{"name": "a.b"}]}
        assert sent["headers"]["X-Key"] == "k"
        assert sent["headers"]["Content-Type"] == "application/json"
        assert sent["timeout"] == 3.0

    def test_a_transport_failure_propagates_to_the_container(self, monkeypatch):
        from stapel_analytics.transport import TransportError

        def fake_post(url, body, headers, timeout=10.0):
            raise TransportError("refused")

        monkeypatch.setattr("stapel_analytics.transport.post_json", fake_post)
        register_adapter(
            "webhook",
            {"handler": deliver_webhook, "enabled": True,
             "config": {"url": "https://collector.example/e"}},
        )
        results = fan_out([{"name": "a.b"}])
        assert results["webhook"]["delivered"] == 0
