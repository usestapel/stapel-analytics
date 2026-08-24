"""The comm bridge: a host's Actions become steps of the same funnels.

This is what makes a funnel able to END in something that happens on a
server — a payment, an email, a webhook delivery — rather than in the last
click before it.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured
from stapel_core.comm import emit

from stapel_analytics.actions import (
    _parse_entry,
    bridged_actions,
    reset_comm_bridge,
    wire_comm_bridge,
)
from stapel_analytics.privacy import hash_user_id
from stapel_analytics.store import iter_events

DECLARED = {
    "EVENTS": {
        "payment_completed": {"description": "paid", "flow": "checkout"},
        "flow.checkout.started": {"description": "started"},
    }
}


@pytest.fixture(autouse=True)
def _bridge_teardown():
    yield
    reset_comm_bridge()


def wire(settings, bridge, **extra):
    settings.STAPEL_ANALYTICS = {**DECLARED, "COMM_BRIDGE": bridge, **extra}
    wire_comm_bridge()


def fire(action, payload):
    """Emit a host Action the way a sibling module would."""
    from django.db import transaction

    with transaction.atomic():
        emit(action, payload)


@pytest.mark.django_db
class TestStringForm:
    def test_a_bridged_action_becomes_an_event(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"amount": 10, "currency": "usd"})
        rows = list(iter_events())
        assert len(rows) == 1
        assert rows[0]["name"] == "payment_completed"

    def test_scalar_payload_keys_become_props(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"amount": 10, "currency": "usd"})
        assert list(iter_events())[0]["props"] == {"amount": 10, "currency": "usd"}

    def test_nested_payload_values_are_dropped(self, settings):
        """A bridged event is a milestone, not a copy of somebody's aggregate."""
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"amount": 10, "items": [{"sku": "x"}]})
        assert list(iter_events())[0]["props"] == {"amount": 10}

    def test_the_server_source_is_recorded(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {})
        assert list(iter_events())[0]["source"] == "server"

    def test_the_source_is_configurable(self, settings):
        wire(settings, {"payment.completed": "payment_completed"},
             SERVER_SOURCE="billing")
        fire("payment.completed", {})
        assert list(iter_events())[0]["source"] == "billing"

    def test_an_unbridged_action_records_nothing(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("something.else", {})
        assert list(iter_events()) == []


@pytest.mark.django_db
class TestIdentity:
    def test_a_user_id_is_hashed_the_frontend_way(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"user_id": "u-1"})
        assert list(iter_events())[0]["user_hash"] == hash_user_id("u-1")

    def test_the_raw_user_id_never_reaches_the_store(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"user_id": "u-1"})
        row = list(iter_events())[0]
        assert "user_id" not in row["props"]
        assert "u-1" not in str(row)

    def test_a_user_hash_is_used_verbatim(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"user_hash": "h" * 64})
        assert list(iter_events())[0]["user_hash"] == "h" * 64

    def test_a_user_hash_wins_over_a_user_id(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"user_id": "u-1", "user_hash": "h" * 64})
        assert list(iter_events())[0]["user_hash"] == "h" * 64

    def test_anon_and_session_ids_are_carried(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        fire("payment.completed", {"anon_id": "a-1", "session_id": "s-1"})
        row = list(iter_events())[0]
        assert row["anon_id"] == "a-1" and row["session_id"] == "s-1"


@pytest.mark.django_db
class TestDictForm:
    def test_a_prop_allowlist_narrows_the_payload(self, settings):
        wire(
            settings,
            {"payment.completed": {"event": "payment_completed",
                                   "props": ["amount"]}},
        )
        fire("payment.completed", {"amount": 10, "internal_ref": "secret"})
        assert list(iter_events())[0]["props"] == {"amount": 10}

    def test_an_allowlisted_key_that_is_absent_is_simply_absent(self, settings):
        wire(settings, {"payment.completed": {"event": "payment_completed",
                                              "props": ["amount", "coupon"]}})
        fire("payment.completed", {"amount": 10})
        assert list(iter_events())[0]["props"] == {"amount": 10}

    def test_a_mapper_shapes_the_props(self, settings):
        wire(
            settings,
            {
                "payment.completed": {
                    "event": "payment_completed",
                    "mapper": "stapel_analytics.tests.test_comm_bridge.to_props",
                }
            },
        )
        fire("payment.completed", {"total_cents": 1500})
        assert list(iter_events())[0]["props"] == {"amount": 15.0}

    def test_a_mapper_returning_none_records_nothing(self, settings):
        wire(
            settings,
            {
                "payment.completed": {
                    "event": "payment_completed",
                    "mapper": "stapel_analytics.tests.test_comm_bridge.skip",
                }
            },
        )
        fire("payment.completed", {"total_cents": 1500})
        assert list(iter_events()) == []

    def test_a_callable_mapper_is_accepted(self, settings):
        wire(
            settings,
            {"payment.completed": {"event": "payment_completed",
                                   "mapper": to_props}},
        )
        fire("payment.completed", {"total_cents": 100})
        assert list(iter_events())[0]["props"] == {"amount": 1.0}

    def test_the_name_key_is_a_synonym_for_event(self, settings):
        wire(settings, {"payment.completed": {"name": "payment_completed"}})
        fire("payment.completed", {})
        assert list(iter_events())[0]["name"] == "payment_completed"


def to_props(payload):
    return {"amount": payload["total_cents"] / 100}


def skip(payload):
    return None


def not_a_dict(payload):
    return ["props"]


class TestConfigurationErrors:
    """Configured-but-broken must be loud: a bridge that quietly does not
    fire looks exactly like a funnel with a bad conversion rate."""

    def test_a_scalar_entry_is_refused(self):
        with pytest.raises(ImproperlyConfigured):
            _parse_entry("a", 7)

    def test_a_dict_without_an_event_is_refused(self):
        with pytest.raises(ImproperlyConfigured):
            _parse_entry("a", {"props": ["x"]})

    def test_an_unimportable_mapper_is_refused(self):
        with pytest.raises(ImproperlyConfigured):
            _parse_entry("a", {"event": "e", "mapper": "no.such.module.fn"})

    def test_a_non_callable_mapper_is_refused(self):
        with pytest.raises(ImproperlyConfigured):
            _parse_entry("a", {"event": "e", "mapper": 7})

    def test_a_scalar_prop_list_is_refused(self):
        with pytest.raises(ImproperlyConfigured):
            _parse_entry("a", {"event": "e", "props": "amount"})

    def test_wiring_a_broken_bridge_raises(self, settings):
        settings.STAPEL_ANALYTICS = {
            **DECLARED, "COMM_BRIDGE": {"a": {"event": "e", "mapper": "no.such"}}
        }
        with pytest.raises(ImproperlyConfigured):
            wire_comm_bridge()

    @pytest.mark.django_db
    def test_a_mapper_returning_a_non_dict_raises(self, settings):
        wire(
            settings,
            {
                "payment.completed": {
                    "event": "payment_completed",
                    "mapper": "stapel_analytics.tests.test_comm_bridge.not_a_dict",
                }
            },
        )
        # comm wraps a failing handler; the cause is the loud refusal.
        from stapel_core.comm.exceptions import ActionDeliveryError

        with pytest.raises(ActionDeliveryError) as exc:
            fire("payment.completed", {})
        assert "expected a dict of props" in str(exc.value)


class TestWiring:
    def test_the_bridge_map_is_readable(self, settings):
        wire(settings, {"payment.completed": "payment_completed"})
        assert list(bridged_actions()) == ["payment.completed"]

    def test_rewiring_replaces_rather_than_stacks(self, settings):
        wire(settings, {"a.one": "payment_completed"})
        wire(settings, {"a.two": "payment_completed"})
        assert list(bridged_actions()) == ["a.two"]

    @pytest.mark.django_db
    def test_a_dropped_action_becomes_inert(self, settings):
        """There is no unsubscribe in the comm registry; the dispatcher
        reads the live map, so a re-wire leaves a stale subscription doing
        nothing rather than firing the old rule."""
        wire(settings, {"payment.completed": "payment_completed"})
        wire(settings, {})
        fire("payment.completed", {})
        assert list(iter_events()) == []

    def test_an_empty_bridge_wires_nothing(self, settings):
        wire(settings, {})
        assert bridged_actions() == {}


@pytest.mark.django_db
class TestFunnelJoin:
    def test_a_bridged_step_completes_a_funnel_started_in_the_browser(
        self, settings, api_client
    ):
        """The whole reason the bridge exists (analytics-standard §1)."""
        from django.utils import timezone

        from stapel_analytics import services

        wire(settings, {"payment.completed": "payment_completed"})
        services.save_funnel(
            slug="checkout", steps=["flow.checkout.started", "payment_completed"]
        )
        user_hash = hash_user_id("u-1")
        api_client.post(
            "/analytics/api/v1/events",
            {
                "events": [
                    {
                        "kind": "track",
                        "name": "flow.checkout.started",
                        "userHash": user_hash,
                        "ts": int(timezone.now().timestamp() * 1000) - 60_000,
                    }
                ]
            },
            format="json",
        )
        fire("payment.completed", {"user_id": "u-1", "amount": 10})

        from stapel_analytics.funnels import funnel_report

        result = funnel_report("checkout")
        assert result.entered == 1 and result.completed == 1
