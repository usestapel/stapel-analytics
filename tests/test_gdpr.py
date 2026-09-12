"""Erasure: analytics rows are user data, and this is the exit.

Both protocols are exercised — the 0.5.0 bus request and the in-process
provider — because a deployment uses one or the other and neither may be
the one that works.
"""
import uuid

import pytest
from django.db import transaction
from stapel_core.comm import emit, on_action

from stapel_analytics import services
from stapel_analytics.erasure import (
    OWNER,
    SUBJECT_TYPES,
    erase,
    erase_account,
    erase_anonymous,
    export_account,
    linked_anon_ids,
)
from stapel_analytics.gdpr import AnalyticsGDPRProvider, erase_subject
from stapel_analytics.privacy import hash_user_id
from stapel_analytics.store import iter_events

USER_ID = "u-1"
OTHER_ID = "u-2"


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {
        "EVENTS": {"a.b": {"description": "x"}, "c.d": {"description": "y"}},
    }


def seed():
    """Two people and an anonymous session that later identified."""
    services.track("a.b", {"k": 1}, user_id=USER_ID)
    services.track("c.d", {"k": 2}, user_id=USER_ID, anon_id="anon-1")
    services.track("a.b", {"k": 3}, anon_id="anon-1")
    services.track("a.b", {"k": 4}, user_id=OTHER_ID)
    services.track("a.b", {"k": 5}, anon_id="anon-2")


@pytest.mark.django_db
class TestOwnerDeclaration:
    def test_the_owner_name_matches_the_provider_section(self):
        assert OWNER == AnalyticsGDPRProvider.section

    def test_the_claimed_subjects_are_account_and_anon(self):
        assert set(SUBJECT_TYPES) == {"account", "anon"}

    def test_the_owner_is_registered_in_this_process(self):
        from stapel_core.gdpr import registered_gdpr_owners

        assert registered_gdpr_owners()["analytics"] == SUBJECT_TYPES


@pytest.mark.django_db
class TestEraseAccount:
    def test_the_persons_rows_go(self):
        seed()
        counts = erase_account(USER_ID)
        assert counts["events"] == 2

    def test_the_linked_anonymous_session_goes_too(self):
        """Before login is the same person's data as after it."""
        seed()
        counts = erase_account(USER_ID)
        assert counts["anonymous_ids"] == 1
        assert counts["anonymous_events"] == 1

    def test_nobody_elses_rows_go(self):
        seed()
        erase_account(USER_ID)
        remaining = {row.get("user_hash") for row in iter_events()}
        assert hash_user_id(OTHER_ID) in remaining
        assert hash_user_id(USER_ID) not in remaining

    def test_an_unlinked_anonymous_session_survives(self):
        seed()
        erase_account(USER_ID)
        anons = {row.get("anon_id") for row in iter_events()}
        assert "anon-2" in anons

    def test_erasure_is_idempotent(self):
        seed()
        erase_account(USER_ID)
        second = erase_account(USER_ID)
        assert second == {
            "events": 0,
            "anonymous_events": 0,
            "anonymous_ids": 0,
            "attribution": 0,
        }

    def test_a_hash_may_be_passed_directly(self):
        seed()
        counts = erase_account(hash_user_id(USER_ID), already_hashed=True)
        assert counts["events"] == 2

    def test_erasing_an_unknown_person_reports_zeros(self):
        seed()
        assert erase_account("nobody")["events"] == 0

    def test_linked_anon_ids_finds_the_link(self):
        seed()
        assert linked_anon_ids(hash_user_id(USER_ID)) == ["anon-1"]

    def test_linked_anon_ids_is_empty_for_a_stranger(self):
        seed()
        assert linked_anon_ids(hash_user_id("nobody")) == []


@pytest.mark.django_db
class TestEraseAnonymous:
    def test_the_sessions_rows_go(self):
        seed()
        assert erase_anonymous("anon-2")["events"] == 1

    def test_other_sessions_survive(self):
        seed()
        erase_anonymous("anon-2")
        assert "anon-1" in {row.get("anon_id") for row in iter_events()}

    def test_it_is_idempotent(self):
        seed()
        erase_anonymous("anon-2")
        assert erase_anonymous("anon-2")["events"] == 0


@pytest.mark.django_db
class TestDispatch:
    def test_erase_routes_account(self):
        seed()
        assert erase("account", USER_ID)["events"] == 2

    def test_erase_routes_anon(self):
        seed()
        assert erase("anon", "anon-2")["events"] == 1

    def test_an_unknown_subject_type_raises(self):
        """A typo must not receipt as an empty success."""
        with pytest.raises(ValueError):
            erase("workspace", "w-1")

    def test_erase_subject_ignores_a_foreign_subject(self):
        """None = 'not mine', so the orchestrator is not falsely confirmed."""
        assert erase_subject("workspace", "w-1") is None

    def test_erase_subject_handles_a_claimed_subject(self):
        seed()
        assert erase_subject("account", USER_ID)["events"] == 2


@pytest.mark.django_db
class TestProvider:
    def test_export_returns_the_rows(self):
        seed()
        exported = export_account(USER_ID)
        assert exported["count"] == 2
        assert exported["user_hash"] == hash_user_id(USER_ID)

    def test_export_serializes_timestamps(self):
        seed()
        assert isinstance(export_account(USER_ID)["events"][0]["ts"], str)

    def test_export_flags_truncation(self, settings):
        seed()
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS,
                                     "MAX_REPORT_EVENTS": 1}
        assert export_account(USER_ID)["truncated"] is True

    def test_the_provider_exports(self):
        seed()
        assert AnalyticsGDPRProvider().export(USER_ID)["count"] == 2

    def test_the_provider_deletes(self):
        seed()
        assert AnalyticsGDPRProvider().delete(USER_ID)["events"] == 2

    def test_anonymize_is_delete(self):
        """There is no useful anonymize for a behavioural row."""
        seed()
        assert AnalyticsGDPRProvider().anonymize(USER_ID)["events"] == 2
        assert hash_user_id(USER_ID) not in {r.get("user_hash") for r in iter_events()}


@pytest.mark.django_db
class TestBusProtocol:
    def test_an_erasure_request_erases_and_receipts(self):
        receipts = []

        @on_action("gdpr.section.erased")
        def collect(event):
            receipts.append(event.payload)

        seed()
        correlation = str(uuid.uuid4())
        with transaction.atomic():
            emit(
                "gdpr.erasure.requested",
                {
                    "correlation_id": correlation,
                    "subject_type": "account",
                    "subject_key": USER_ID,
                },
            )
        assert hash_user_id(USER_ID) not in {r.get("user_hash") for r in iter_events()}
        mine = [r for r in receipts if r.get("owner") == OWNER]
        assert mine and mine[0]["correlation_id"] == correlation
        assert mine[0]["counts"]["events"] == 2

    def test_a_foreign_subject_is_answered_with_silence(self):
        receipts = []

        @on_action("gdpr.section.erased")
        def collect(event):
            receipts.append(event.payload)

        with transaction.atomic():
            emit(
                "gdpr.erasure.requested",
                {
                    "correlation_id": str(uuid.uuid4()),
                    "subject_type": "workspace",
                    "subject_key": "w-1",
                },
            )
        assert [r for r in receipts if r.get("owner") == OWNER] == []

    def test_the_owner_probe_is_answered(self):
        answers = []

        @on_action("gdpr.owner.alive")
        def collect(event):
            answers.append(event.payload)

        correlation = str(uuid.uuid4())
        with transaction.atomic():
            emit("gdpr.owner.probe", {"correlation_id": correlation})
        mine = [a for a in answers if a.get("owner") == OWNER]
        assert mine
        assert set(mine[0]["subject_types"]) == set(SUBJECT_TYPES)

    def test_the_legacy_user_deleted_signal_erases(self):
        seed()
        with transaction.atomic():
            emit("user.deleted", {"user_id": USER_ID})
        assert hash_user_id(USER_ID) not in {r.get("user_hash") for r in iter_events()}

    def test_a_redelivered_request_receipts_zeros(self):
        receipts = []

        @on_action("gdpr.section.erased")
        def collect(event):
            receipts.append(event.payload)

        seed()
        correlation = str(uuid.uuid4())
        for _ in range(2):
            with transaction.atomic():
                emit(
                    "gdpr.erasure.requested",
                    {
                        "correlation_id": correlation,
                        "subject_type": "account",
                        "subject_key": USER_ID,
                    },
                )
        mine = [r for r in receipts if r.get("owner") == OWNER]
        assert mine[1]["counts"]["events"] == 0
        # A deterministic receipt id: one erasure, one proof.
        assert mine[0]["receipt_id"] == mine[1]["receipt_id"]

    def test_an_anon_subject_is_erased_over_the_bus(self):
        seed()
        with transaction.atomic():
            emit(
                "gdpr.erasure.requested",
                {
                    "correlation_id": str(uuid.uuid4()),
                    "subject_type": "anon",
                    "subject_key": "anon-2",
                },
            )
        assert "anon-2" not in {r.get("anon_id") for r in iter_events()}
