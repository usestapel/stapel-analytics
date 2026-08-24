"""The event registry: merge order, patterns, removal, file loading."""
import json

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from stapel_analytics.registry import (
    BUILTIN_EVENTS,
    declared_events,
    event_registry,
    funnel_of,
    is_registered,
    normalize_definitions,
    register_event,
    registry_mode,
    reset_events,
    resolve_event,
)


class TestBuiltins:
    def test_builtins_are_registered_without_configuration(self):
        assert is_registered("identify")

    def test_flow_pattern_matches_any_flow_step(self):
        assert is_registered("flow.checkout.started")
        assert is_registered("flow.signup.completed")

    def test_flow_pattern_does_not_match_a_lookalike(self):
        assert not is_registered("flowchart.opened")

    def test_builtins_are_not_declared_events(self):
        assert declared_events() == {}

    def test_registry_contains_the_builtins(self):
        registry = event_registry()
        for name in BUILTIN_EVENTS:
            assert name in registry


class TestSettingsLayer:
    @override_settings(STAPEL_ANALYTICS={"EVENTS": {"listing.published": {"description": "x"}}})
    def test_settings_events_register(self):
        assert is_registered("listing.published")

    @override_settings(STAPEL_ANALYTICS={"EVENTS": {"listing.published": {"description": "x"}}})
    def test_settings_events_are_declared(self):
        assert "listing.published" in declared_events()

    @override_settings(STAPEL_ANALYTICS={"EVENTS": {"identify": None}})
    def test_none_removes_a_builtin(self):
        assert not is_registered("identify")

    @override_settings(STAPEL_ANALYTICS={"EVENTS": {"flow.*": None}})
    def test_none_removes_a_builtin_pattern(self):
        assert not is_registered("flow.checkout.started")

    @override_settings(
        STAPEL_ANALYTICS={"EVENTS": [{"name": "a.b", "description": "from a list"}]}
    )
    def test_a_list_of_definitions_is_accepted(self):
        """events.json is a LIST; a settings map is a MAP. Both are legal."""
        assert resolve_event("a.b")["description"] == "from a list"

    @override_settings(STAPEL_ANALYTICS={"EVENTS": {"a.b": {}}})
    def test_name_is_injected_from_the_map_key(self):
        assert resolve_event("a.b")["name"] == "a.b"

    @override_settings(STAPEL_ANALYTICS={"EVENTS": {"a.b": "not-a-definition"}})
    def test_a_scalar_definition_is_refused(self):
        with pytest.raises(TypeError):
            event_registry()

    @override_settings(STAPEL_ANALYTICS={"EVENTS": 7})
    def test_a_scalar_registry_is_refused(self):
        with pytest.raises(TypeError):
            event_registry()


class TestRuntimeLayer:
    def test_runtime_registration_wins_over_settings(self):
        with override_settings(STAPEL_ANALYTICS={"EVENTS": {"a.b": {"description": "s"}}}):
            register_event("a.b", {"description": "runtime"})
            assert resolve_event("a.b")["description"] == "runtime"

    def test_runtime_none_removes_a_settings_event(self):
        with override_settings(STAPEL_ANALYTICS={"EVENTS": {"a.b": {"description": "s"}}}):
            register_event("a.b", None)
            assert not is_registered("a.b")

    def test_reset_drops_runtime_registrations(self):
        register_event("only.runtime", {"description": "x"})
        assert is_registered("only.runtime")
        reset_events()
        assert not is_registered("only.runtime")


class TestPatterns:
    @override_settings(
        STAPEL_ANALYTICS={
            "EVENTS": {
                "flow.checkout.*": {"description": "checkout steps"},
            }
        }
    )
    def test_the_longest_matching_pattern_wins(self):
        assert resolve_event("flow.checkout.paid")["description"] == "checkout steps"
        assert resolve_event("flow.signup.paid")["description"] != "checkout steps"

    @override_settings(
        STAPEL_ANALYTICS={
            "EVENTS": {
                "flow.checkout.paid": {"description": "exact"},
                "flow.checkout.*": {"description": "pattern"},
            }
        }
    )
    def test_an_exact_name_beats_a_pattern(self):
        assert resolve_event("flow.checkout.paid")["description"] == "exact"

    def test_an_unmatched_name_resolves_to_none(self):
        assert resolve_event("nothing.declares.this") is None


class TestFunnelMembership:
    @override_settings(
        STAPEL_ANALYTICS={"EVENTS": {"a.b": {"description": "x", "flow": "checkout"}}}
    )
    def test_flow_key_is_read(self):
        assert funnel_of("a.b") == "checkout"

    @override_settings(
        STAPEL_ANALYTICS={"EVENTS": {"a.b": {"description": "x", "funnel": "checkout"}}}
    )
    def test_funnel_is_accepted_as_a_synonym(self):
        assert funnel_of("a.b") == "checkout"

    def test_an_event_without_a_flow_answers_none(self):
        assert funnel_of("identify") is None


class TestEventsFile:
    def _write(self, tmp_path, payload):
        path = tmp_path / "events.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_a_bare_list_file_loads(self, tmp_path):
        path = self._write(tmp_path, [{"name": "file.event", "description": "d"}])
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": path}):
            assert is_registered("file.event")

    def test_a_gen_events_envelope_loads(self, tmp_path):
        """`gen:events` writes {"events": [...]} — the envelope is unwrapped."""
        path = self._write(tmp_path, {"events": [{"name": "file.event"}]})
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": path}):
            assert is_registered("file.event")

    def test_settings_events_win_over_the_file(self, tmp_path):
        path = self._write(tmp_path, [{"name": "e", "description": "file"}])
        with override_settings(
            STAPEL_ANALYTICS={"EVENTS_FILE": path, "EVENTS": {"e": {"description": "settings"}}}
        ):
            assert resolve_event("e")["description"] == "settings"

    def test_settings_none_removes_a_file_event(self, tmp_path):
        path = self._write(tmp_path, [{"name": "e"}])
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": path, "EVENTS": {"e": None}}):
            assert not is_registered("e")

    def test_a_missing_file_is_loud(self):
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": "/nonexistent/events.json"}):
            with pytest.raises(ImproperlyConfigured):
                event_registry()

    def test_a_malformed_file_is_loud(self, tmp_path):
        path = tmp_path / "events.json"
        path.write_text("{not json", encoding="utf-8")
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": str(path)}):
            with pytest.raises(ImproperlyConfigured):
                event_registry()

    def test_a_list_entry_without_a_name_is_refused(self, tmp_path):
        path = self._write(tmp_path, [{"description": "nameless"}])
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": path}):
            with pytest.raises(TypeError):
                event_registry()

    def test_the_file_is_read_once(self, tmp_path):
        path = self._write(tmp_path, [{"name": "cached"}])
        with override_settings(STAPEL_ANALYTICS={"EVENTS_FILE": path}):
            assert is_registered("cached")
            (tmp_path / "events.json").unlink()
            # Still answers: the parse is cached, so ingest does not read a
            # file per batch.
            assert is_registered("cached")


class TestNormalizeDefinitions:
    def test_empty_input_is_empty_output(self):
        assert normalize_definitions(None) == {}
        assert normalize_definitions({}) == {}
        assert normalize_definitions([]) == {}

    def test_a_tuple_is_accepted(self):
        assert "x" in normalize_definitions(({"name": "x"},))

    def test_a_scalar_is_refused(self):
        with pytest.raises(TypeError):
            normalize_definitions("events")


class TestMode:
    def test_default_mode_is_warn(self):
        assert registry_mode() == "warn"

    @override_settings(STAPEL_ANALYTICS={"REGISTRY_MODE": "REJECT"})
    def test_mode_is_case_insensitive(self):
        assert registry_mode() == "reject"

    @override_settings(STAPEL_ANALYTICS={"REGISTRY_MODE": "nonsense"})
    def test_an_unknown_mode_falls_back_to_warn(self):
        assert registry_mode() == "warn"

    @override_settings(STAPEL_ANALYTICS={"REGISTRY_MODE": "off"})
    def test_off_is_honoured(self):
        assert registry_mode() == "off"
