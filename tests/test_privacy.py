"""The PII guard and the user hash — the two privacy primitives.

The guard's heuristics must agree with ``@stapel/analytics``' ``pii.ts``:
the browser redacts what this refuses, so a value that survives one and not
the other means a deployment whose two tiers disagree about what personal
data is.
"""
import hashlib

import pytest
from django.test import override_settings

from stapel_analytics.privacy import (
    PII_REDACTED,
    PiiRefused,
    guard_props,
    hash_user_id,
    looks_like_pii,
    pii_mode,
)


class TestHeuristics:
    @pytest.mark.parametrize(
        "value",
        [
            "someone@example.com",
            "  someone@example.co.uk ",
            "reply to me at a.b+tag@sub.example.org please",
            "+1 (555) 010-9999",
            "555-0100999",
            "5550100999",
            "+49 30 1234567",
        ],
    )
    def test_pii_shapes_are_detected(self, value):
        assert looks_like_pii(value)

    @pytest.mark.parametrize(
        "value",
        [
            "listing-42",
            "checkout",
            "1234",              # too few digits for a phone
            "berlin",
            "",
            "a@b",               # no TLD
        ],
    )
    def test_non_pii_is_left_alone(self, value):
        assert not looks_like_pii(value)


class TestIsoDateExemption:
    """An ISO date matches the phone SHAPE and is not a phone number.

    A one-directional refinement over the frontend heuristic: the browser
    still redacts these, so the two tiers never disagree about a value that
    actually travels — but a server-side ``track()`` with a date prop is no
    longer dropped under the default ``reject`` mode.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "2026-08-24",
            "2026-08-24T10:00:00",
            "2026-08-24T10:00:00Z",
            "2026-08-24T10:00:00.123+02:00",
            "2026-08-24 10:00",
        ],
    )
    def test_iso_timestamps_are_not_pii(self, value):
        assert not looks_like_pii(value)

    def test_a_real_phone_is_still_pii(self):
        assert looks_like_pii("2026-0824-99")


class TestGuardModes:
    def test_default_mode_is_reject(self):
        assert pii_mode() == "reject"

    @override_settings(STAPEL_ANALYTICS={"PII_MODE": "STRIP"})
    def test_mode_is_case_insensitive(self):
        assert pii_mode() == "strip"

    @override_settings(STAPEL_ANALYTICS={"PII_MODE": "nonsense"})
    def test_an_unknown_mode_falls_back_to_reject(self):
        assert pii_mode() == "reject"

    def test_reject_raises_and_names_the_prop(self):
        with pytest.raises(PiiRefused) as exc:
            guard_props({"email": "a@b.com"}, event_name="e")
        assert exc.value.path == "props.email"

    @override_settings(STAPEL_ANALYTICS={"PII_MODE": "strip"})
    def test_strip_redacts_the_value(self):
        assert guard_props({"email": "a@b.com"}, event_name="e") == {
            "email": PII_REDACTED
        }

    @override_settings(STAPEL_ANALYTICS={"PII_MODE": "warn"})
    def test_warn_keeps_the_value(self):
        assert guard_props({"email": "a@b.com"}, event_name="e") == {
            "email": "a@b.com"
        }

    @override_settings(STAPEL_ANALYTICS={"PII_MODE": "off"})
    def test_off_short_circuits(self):
        props = {"email": "a@b.com"}
        assert guard_props(props, event_name="e") is props

    def test_keys_are_never_judged(self):
        """`{"email_verified": true}` is not PII; the value is what matters."""
        assert guard_props({"email_verified": True}, event_name="e") == {
            "email_verified": True
        }


class TestNesting:
    def test_a_nested_dict_is_walked(self):
        with pytest.raises(PiiRefused) as exc:
            guard_props({"contact": {"phone": "+1 555 010 9999"}}, event_name="e")
        assert exc.value.path == "props.contact.phone"

    def test_a_list_is_walked(self):
        with pytest.raises(PiiRefused) as exc:
            guard_props({"cc": ["ok", "a@b.com"]}, event_name="e")
        assert exc.value.path == "props.cc[1]"

    def test_a_tuple_is_walked_and_becomes_a_list(self):
        with override_settings(STAPEL_ANALYTICS={"PII_MODE": "strip"}):
            assert guard_props({"cc": ("a@b.com",)}, event_name="e") == {
                "cc": [PII_REDACTED]
            }

    @override_settings(STAPEL_ANALYTICS={"PII_MODE": "strip"})
    def test_deep_nesting_is_reached(self):
        guarded = guard_props(
            {"a": {"b": [{"c": "a@b.com"}]}}, event_name="e"
        )
        assert guarded["a"]["b"][0]["c"] == PII_REDACTED

    def test_non_string_scalars_pass_through(self):
        assert guard_props(
            {"n": 1, "f": 1.5, "b": True, "none": None}, event_name="e"
        ) == {"n": 1, "f": 1.5, "b": True, "none": None}

    def test_empty_props_short_circuit(self):
        assert guard_props({}, event_name="e") == {}


class TestGuardSeam:
    def test_the_guard_is_swappable(self):
        with override_settings(
            STAPEL_ANALYTICS={
                "PII_GUARD": "stapel_analytics.tests.test_privacy.everything_is_pii"
            }
        ):
            with pytest.raises(PiiRefused):
                guard_props({"x": "harmless"}, event_name="e")


def everything_is_pii(value: str) -> bool:
    """A deliberately paranoid guard, used to prove the seam is live."""
    return True


class TestUserHash:
    def test_hash_matches_the_frontend_unsalted_sha256(self):
        """`@stapel/analytics` hash.ts: sha256Hex(userId), no salt.

        This equality is the whole reason a server-side funnel step lands on
        the same subject as the clicks that preceded it.
        """
        user_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        assert hash_user_id(user_id) == hashlib.sha256(user_id.encode()).hexdigest()

    def test_a_non_string_id_is_stringified(self):
        assert hash_user_id(42) == hashlib.sha256(b"42").hexdigest()

    def test_the_hash_is_stable(self):
        assert hash_user_id("u") == hash_user_id("u")

    @override_settings(STAPEL_ANALYTICS={"USER_HASH_SALT": "pepper"})
    def test_a_salt_changes_the_hash(self):
        assert hash_user_id("u") == hashlib.sha256(b"pepperu").hexdigest()

    @override_settings(STAPEL_ANALYTICS={"USER_HASH_SALT": "pepper"})
    def test_a_salt_breaks_the_frontend_join(self):
        assert hash_user_id("u") != hashlib.sha256(b"u").hexdigest()
