"""Normalization: what the wire says, and what gets stored."""
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_analytics.ingest import (
    IngestRefused,
    Rejection,
    default_subject,
    normalize_batch,
    resolve_source,
)

DECLARED = {"EVENTS": {"listing.published": {"description": "x"}}}


def _now_ms(offset_seconds: int = 0) -> int:
    return int((timezone.now() + timedelta(seconds=offset_seconds)).timestamp() * 1000)


def _batch(*events, **extra):
    return {"events": list(events), **extra}


@pytest.fixture(autouse=True)
def _declared(settings):
    """The suite installs only this module, so its registry is the built-ins.

    Every test here fires named events, so the module declares a small
    vocabulary the way a project's events.json would. Tests that need a
    different configuration layer ``override_settings`` on top — which
    REPLACES this dict, hence the ``**DECLARED`` spread in each of them.
    """
    settings.STAPEL_ANALYTICS = dict(DECLARED)


def _event(**overrides):
    base = {
        "id": "1755000000000-1",
        "kind": "track",
        "name": "listing.published",
        "props": {},
        "ts": _now_ms(),
    }
    base.update(overrides)
    return base


class TestBatchShape:
    def test_a_valid_batch_normalizes(self):
        events, rejections = normalize_batch(_batch(_event()))
        assert len(events) == 1 and rejections == []

    def test_an_empty_batch_is_legal(self):
        events, rejections = normalize_batch(_batch())
        assert events == [] and rejections == []

    def test_a_non_object_body_is_refused(self):
        with pytest.raises(IngestRefused) as exc:
            normalize_batch(["not", "a", "batch"])
        assert exc.value.error_key == "batch_not_object"

    def test_a_body_without_events_is_refused(self):
        with pytest.raises(IngestRefused) as exc:
            normalize_batch({})
        assert exc.value.error_key == "batch_missing_events"

    def test_events_must_be_a_list(self):
        with pytest.raises(IngestRefused) as exc:
            normalize_batch({"events": {"not": "a list"}})
        assert exc.value.error_key == "batch_events_not_list"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "MAX_BATCH_SIZE": 2})
    def test_an_oversized_batch_is_refused_whole(self):
        with pytest.raises(IngestRefused) as exc:
            normalize_batch(_batch(_event(), _event(), _event()))
        assert exc.value.error_key == "batch_too_large"
        assert exc.value.params == {"max": 2, "got": 3}


class TestPartialAcceptance:
    def test_one_bad_event_does_not_condemn_the_batch(self):
        events, rejections = normalize_batch(
            _batch(_event(), _event(name=None), _event())
        )
        assert len(events) == 2
        assert [r.reason for r in rejections] == ["missing_name"]

    def test_a_rejection_carries_its_index(self):
        _, rejections = normalize_batch(_batch(_event(), _event(kind="nonsense")))
        assert rejections[0].index == 1

    def test_a_non_object_event_is_rejected_alone(self):
        events, rejections = normalize_batch(_batch(_event(), "not an event"))
        assert len(events) == 1
        assert rejections[0].reason == "not_an_object"


class TestKinds:
    def test_track_is_the_default_kind(self):
        events, _ = normalize_batch(_batch(_event(kind=None)))
        assert events[0].kind == "track"

    def test_page_events_skip_the_registry(self):
        """A page name is a path chosen at render time, not a vocabulary."""
        events, rejections = normalize_batch(
            _batch(_event(kind="page", name="/listings/42"))
        )
        assert rejections == [] and events[0].unregistered is False

    def test_identify_defaults_its_name(self):
        events, _ = normalize_batch(_batch({"kind": "identify", "ts": _now_ms()}))
        assert events[0].name == "identify"

    def test_an_unknown_kind_is_rejected(self):
        _, rejections = normalize_batch(_batch(_event(kind="scream")))
        assert rejections[0].reason == "unknown_kind"
        assert rejections[0].detail == "scream"

    def test_kind_is_case_insensitive(self):
        events, _ = normalize_batch(_batch(_event(kind="TRACK")))
        assert events[0].kind == "track"


class TestNames:
    def test_the_event_alias_is_read(self):
        events, _ = normalize_batch(
            _batch({"kind": "track", "event": "listing.published", "ts": _now_ms()})
        )
        assert events[0].name == "listing.published"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "MAX_NAME_LENGTH": 5})
    def test_an_overlong_name_is_rejected(self):
        _, rejections = normalize_batch(_batch(_event(name="way.too.long")))
        assert rejections[0].reason == "name_too_long"

    def test_a_missing_name_on_a_track_is_rejected(self):
        _, rejections = normalize_batch(_batch({"kind": "track", "ts": _now_ms()}))
        assert rejections[0].reason == "missing_name"


class TestProps:
    def test_props_default_to_empty(self):
        events, _ = normalize_batch(_batch(_event(props=None)))
        assert events[0].props == {}

    def test_traits_are_read_as_props(self):
        """identify() sends `traits`; the store has one props column."""
        events, _ = normalize_batch(
            _batch({"kind": "identify", "traits": {"plan": "pro"}, "ts": _now_ms()})
        )
        assert events[0].props == {"plan": "pro"}

    def test_non_object_props_are_rejected(self):
        _, rejections = normalize_batch(_batch(_event(props=["a"])))
        assert rejections[0].reason == "props_not_an_object"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "MAX_PROPS_BYTES": 16})
    def test_oversized_props_are_rejected(self):
        _, rejections = normalize_batch(_batch(_event(props={"k": "v" * 100})))
        assert rejections[0].reason == "props_too_large"
        assert ">" in rejections[0].detail

    def test_pii_props_are_rejected_by_default(self):
        _, rejections = normalize_batch(_batch(_event(props={"email": "a@b.com"})))
        assert rejections[0].reason == "pii"
        assert rejections[0].detail == "props.email"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "PII_MODE": "strip"})
    def test_strip_mode_keeps_the_event(self):
        events, rejections = normalize_batch(_batch(_event(props={"email": "a@b.com"})))
        assert rejections == []
        assert events[0].props["email"] == "[redacted]"


class TestTimestamps:
    def test_epoch_milliseconds_are_read(self):
        ms = _now_ms(-60)
        events, _ = normalize_batch(_batch(_event(ts=ms)))
        assert abs(events[0].ts.timestamp() * 1000 - ms) < 1000

    def test_an_iso_string_is_read(self):
        moment = timezone.now() - timedelta(minutes=1)
        events, _ = normalize_batch(_batch(_event(ts=moment.isoformat())))
        assert abs((events[0].ts - moment).total_seconds()) < 1

    def test_a_z_suffixed_iso_string_is_read(self):
        moment = timezone.now() - timedelta(minutes=1)
        stamp = moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        events, _ = normalize_batch(_batch(_event(ts=stamp)))
        assert abs((events[0].ts - moment).total_seconds()) < 2

    def test_a_naive_iso_string_is_assumed_utc(self):
        moment = timezone.now() - timedelta(minutes=1)
        events, _ = normalize_batch(
            _batch(_event(ts=moment.replace(tzinfo=None).isoformat()))
        )
        assert events[0].ts.tzinfo is not None

    def test_a_missing_ts_becomes_server_time(self):
        events, _ = normalize_batch(_batch(_event(ts=None)))
        assert abs((events[0].ts - timezone.now()).total_seconds()) < 5

    def test_a_future_clock_is_corrected_not_refused(self):
        """Browser clocks are wrong; dropping those users biases every funnel."""
        events, rejections = normalize_batch(_batch(_event(ts=_now_ms(86400))))
        assert rejections == []
        assert abs((events[0].ts - timezone.now()).total_seconds()) < 5

    def test_a_clock_within_the_skew_is_kept(self):
        ahead = _now_ms(60)
        events, _ = normalize_batch(_batch(_event(ts=ahead)))
        assert abs(events[0].ts.timestamp() * 1000 - ahead) < 1000

    def test_an_ancient_event_is_refused(self):
        _, rejections = normalize_batch(_batch(_event(ts=_now_ms(-86400 * 30))))
        assert rejections[0].reason == "too_old"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "MAX_EVENT_AGE_SECONDS": 0})
    def test_the_age_bound_can_be_disabled(self):
        events, rejections = normalize_batch(_batch(_event(ts=_now_ms(-86400 * 3650))))
        assert rejections == [] and len(events) == 1

    def test_an_unreadable_ts_is_rejected(self):
        _, rejections = normalize_batch(_batch(_event(ts="not a date")))
        assert rejections[0].reason == "invalid_ts"

    def test_a_boolean_ts_is_rejected(self):
        _, rejections = normalize_batch(_batch(_event(ts=True)))
        assert rejections[0].reason == "invalid_ts"

    def test_the_timestamp_alias_is_read(self):
        ms = _now_ms(-30)
        events, _ = normalize_batch(
            _batch({"kind": "track", "name": "listing.published", "timestamp": ms})
        )
        assert abs(events[0].ts.timestamp() * 1000 - ms) < 1000


class TestIdentity:
    def test_camel_case_user_hash_is_read(self):
        events, _ = normalize_batch(_batch(_event(userHash="a" * 64)))
        assert events[0].user_hash == "a" * 64

    def test_snake_case_user_hash_is_read(self):
        events, _ = normalize_batch(_batch(_event(user_hash="b" * 64)))
        assert events[0].user_hash == "b" * 64

    def test_batch_level_anon_id_applies_to_every_event(self):
        events, _ = normalize_batch(_batch(_event(), _event(), anon_id="visitor-1"))
        assert [e.anon_id for e in events] == ["visitor-1", "visitor-1"]

    def test_an_event_overrides_the_batch_anon_id(self):
        events, _ = normalize_batch(
            _batch(_event(anonId="specific"), anon_id="visitor-1")
        )
        assert events[0].anon_id == "specific"

    def test_batch_level_session_id_applies(self):
        events, _ = normalize_batch(_batch(_event(), session_id="s-1"))
        assert events[0].session_id == "s-1"

    def test_camel_case_session_id_is_read(self):
        events, _ = normalize_batch(_batch(_event(sessionId="s-2")))
        assert events[0].session_id == "s-2"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "MAX_ID_LENGTH": 4})
    def test_an_overlong_id_is_truncated_not_refused(self):
        events, _ = normalize_batch(_batch(_event(anon_id="0123456789")))
        assert events[0].anon_id == "0123"

    def test_the_facade_event_id_is_kept(self):
        events, _ = normalize_batch(_batch(_event(id="1755-7")))
        assert events[0].event_id == "1755-7"


class TestRegistryEnforcement:
    def test_an_unregistered_event_is_stored_and_marked(self):
        events, rejections = normalize_batch(_batch(_event(name="nobody.declared")))
        assert rejections == []
        assert events[0].unregistered is True

    def test_a_registered_event_is_not_marked(self):
        events, _ = normalize_batch(_batch(_event()))
        assert events[0].unregistered is False

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "REGISTRY_MODE": "reject"})
    def test_reject_mode_refuses_the_event(self):
        events, rejections = normalize_batch(_batch(_event(name="nobody.declared")))
        assert events == []
        assert rejections[0].reason == "unregistered"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "REGISTRY_MODE": "off"})
    def test_off_mode_marks_nothing(self):
        events, _ = normalize_batch(_batch(_event(name="nobody.declared")))
        assert events[0].unregistered is False


class TestWriteKeySource:
    def test_an_unknown_key_falls_back_to_the_default_source(self):
        source, recognized = resolve_source("nope")
        assert (source, recognized) == ("web", False)

    @override_settings(STAPEL_ANALYTICS={"WRITE_KEYS": {"k1": "ios"}})
    def test_a_known_key_names_its_source(self):
        assert resolve_source("k1") == ("ios", True)

    @override_settings(STAPEL_ANALYTICS={"DEFAULT_SOURCE": "spa"})
    def test_the_default_source_is_configurable(self):
        assert resolve_source(None)[0] == "spa"

    @override_settings(STAPEL_ANALYTICS={**DECLARED, "WRITE_KEYS": {"k1": "ios"}})
    def test_the_batch_source_reaches_every_event(self):
        events, _ = normalize_batch(_batch(_event()), default_source="ios")
        assert events[0].source == "ios"

    def test_an_event_may_name_its_own_source(self):
        events, _ = normalize_batch(_batch(_event(source="worker")))
        assert events[0].source == "worker"


class TestPayloadShape:
    def test_absent_identity_fields_are_absent_from_the_payload(self):
        """A false boolean in every row of an unbounded table is bytes."""
        events, _ = normalize_batch(_batch(_event()))
        payload = events[0].payload()
        assert "unregistered" not in payload
        assert "user_hash" not in payload
        assert set(payload) == {"name", "kind", "props", "source", "event_id"}

    def test_present_identity_fields_are_written(self):
        events, _ = normalize_batch(
            _batch(_event(userHash="h", anonId="a", sessionId="s"))
        )
        payload = events[0].payload()
        assert payload["user_hash"] == "h"
        assert payload["anon_id"] == "a"
        assert payload["session_id"] == "s"

    def test_unregistered_is_written_when_true(self):
        events, _ = normalize_batch(_batch(_event(name="nobody.declared")))
        assert events[0].payload()["unregistered"] is True


class TestSubjectResolution:
    def test_user_hash_wins(self):
        assert default_subject({"user_hash": "h", "anon_id": "a"}) == "h"

    def test_anon_id_is_next(self):
        assert default_subject({"anon_id": "a", "session_id": "s"}) == "a"

    def test_session_id_is_last(self):
        assert default_subject({"session_id": "s"}) == "s"

    def test_a_subjectless_row_answers_none(self):
        assert default_subject({"name": "x"}) is None


class TestRejectionSerialization:
    def test_detail_is_omitted_when_empty(self):
        assert Rejection(0, "e", "pii").as_dict() == {
            "index": 0, "name": "e", "reason": "pii"
        }

    def test_detail_is_included_when_present(self):
        assert Rejection(1, "e", "pii", "props.x").as_dict()["detail"] == "props.x"
