"""A merge is not a delete — the other half of an account's life cycle.

stapel-auth folds an anonymous guest into an existing account on sign-in and
then deletes the guest row. This module answered ``user.deleted`` from its
first release and said nothing about ``user.merged``, and silence there is
not neutrality — it is a wrong answer given quietly.

Both halves of what this module owns carry over: ``Funnel.owner_id`` in the
platform database, and every event row's ``user_hash`` in the store, through
``eventstore.rekey`` (core 0.54.0). The stream half used to be a documented
gap pinned by a test; that test is now the assertion that it is closed.
"""
import uuid
from types import SimpleNamespace

import pytest

from stapel_core.comm import action_registry

from stapel_analytics import services
from stapel_analytics.actions import handle_user_merged
from stapel_analytics.models import Funnel
from stapel_analytics.privacy import hash_user_id
from stapel_analytics.store import iter_events

GUEST = str(uuid.uuid4())
SURVIVOR = str(uuid.uuid4())


def _event(**payload):
    return SimpleNamespace(payload=payload, event_id="evt-merge-1", service="auth")


def _funnel(owner_id, slug):
    return Funnel.objects.create(
        slug=slug, title=slug, steps=["a.b", "c.d"], owner_id=owner_id
    )


@pytest.mark.django_db
class TestSubscription:
    def test_user_merged_is_subscribed(self):
        """The pair the lifecycle check reads: this module answers both."""
        assert action_registry.handlers("user.deleted")
        assert handle_user_merged in action_registry.handlers("user.merged")

    def test_the_lifecycle_pair_check_is_green(self):
        """``stapel_core.lifecycle.E001`` with this app loaded and ready().

        The ``user.deleted`` half is a closure stapel-core subscribes on this
        library's behalf from ``register_gdpr_owner``; core stamps it with
        this module's name, so the pair is charged here and not to core. If
        that stamp ever stops working this assertion turns red in THIS repo,
        which is where somebody can act on it.
        """
        from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs

        assert check_lifecycle_pairs() == []


@pytest.mark.django_db
class TestFunnelsFollowTheSurvivor:
    def test_a_funnel_authored_as_a_guest_lands_on_the_survivor(self):
        """Otherwise the survivor gets 403 on their own funnel: the API reads
        an unowned funnel as an operator's object, not a user's."""
        mine = _funnel(GUEST, "guest-funnel")
        theirs = _funnel(str(uuid.uuid4()), "someone-elses")

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert str(Funnel.objects.get(pk=mine.pk).owner_id) == SURVIVOR
        assert not Funnel.objects.filter(owner_id=GUEST).exists()
        # Nobody else's funnel moved.
        assert str(Funnel.objects.get(pk=theirs.pk).owner_id) == theirs.owner_id

    def test_redelivery_changes_nothing_further(self):
        """Delivery is at-least-once; the second run is the idempotency path."""
        _funnel(GUEST, "guest-funnel")
        payload = _event(from_user_id=GUEST, into_user_id=SURVIVOR)

        handle_user_merged(payload)
        after_first = sorted(Funnel.objects.values_list("id", "owner_id"))

        handle_user_merged(payload)

        assert after_first == sorted(Funnel.objects.values_list("id", "owner_id"))

    def test_a_guest_this_module_never_saw_is_a_quiet_no_op(self):
        """Most merges name a guest who never authored anything here."""
        theirs = _funnel(str(uuid.uuid4()), "someone-elses")

        handle_user_merged(
            _event(from_user_id=str(uuid.uuid4()), into_user_id=SURVIVOR)
        )

        assert not Funnel.objects.filter(owner_id=SURVIVOR).exists()
        assert str(Funnel.objects.get(pk=theirs.pk).owner_id) == theirs.owner_id

    def test_merging_an_account_into_itself_does_nothing(self):
        mine = _funnel(GUEST, "guest-funnel")

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=GUEST))

        assert str(Funnel.objects.get(pk=mine.pk).owner_id) == GUEST


@pytest.mark.django_db
class TestPoisonPayloads:
    def test_a_malformed_id_is_logged_and_dropped(self):
        """``UUIDField`` raises ``ValidationError``, which is not a ``ValueError``.

        An escaping exception is a poison pill: no redelivery can fix a typo,
        so the bus would replay it until it gives up.
        """
        mine = _funnel(GUEST, "guest-funnel")

        handle_user_merged(_event(from_user_id="not-a-uuid", into_user_id=SURVIVOR))
        handle_user_merged(_event(from_user_id=GUEST, into_user_id="not-a-uuid"))

        assert str(Funnel.objects.get(pk=mine.pk).owner_id) == GUEST

    def test_a_payload_missing_an_id_does_not_raise(self):
        mine = _funnel(GUEST, "guest-funnel")

        handle_user_merged(_event(into_user_id=SURVIVOR))
        handle_user_merged(_event(from_user_id=GUEST))
        handle_user_merged(_event())

        assert str(Funnel.objects.get(pk=mine.pk).owner_id) == GUEST


@pytest.mark.django_db
class TestTheEventStreamFollowsTheSurvivor:
    """The gap 0.2.0 documented, now closed.

    This class used to be ``TestTheEventStreamIsNotReKeyed`` and asserted the
    opposite: the guest's rows kept their own ``user_hash`` because
    ``stapel_core.eventstore`` had no update, and the only way to re-parent
    them was read, append-under-the-new-hash, purge-the-old — three calls
    with no transaction across them, which under at-least-once delivery
    counts one person's history twice, permanently. The pin said that if core
    ever grew an atomic subject re-key, this class is what has to change.

    Core 0.54.0 grew it. So the assertions are inverted, and the one that did
    not need inverting — that no row is ever duplicated — stays exactly as it
    was, because it was never a statement about the gap. It was the property
    the gap existed to protect, and it still holds now that the gap is shut.
    """

    @pytest.fixture(autouse=True)
    def _declared(self, settings):
        settings.STAPEL_ANALYTICS = {"EVENTS": {"a.b": {"description": "x"}}}

    def test_the_guests_rows_become_the_survivors(self):
        services.track("a.b", {"k": 1}, user_id=GUEST)
        services.track("a.b", {"k": 2}, user_id=SURVIVOR)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        guest_hash = hash_user_id(GUEST)
        survivor_hash = hash_user_id(SURVIVOR)
        assert list(iter_events(filters={"user_hash": guest_hash})) == []
        survived = list(iter_events(filters={"user_hash": survivor_hash}))
        assert sorted(e["props"]["k"] for e in survived) == [1, 2]

    def test_no_row_is_duplicated_by_the_merge(self):
        """The property the gap existed to protect. Unchanged, still true."""
        services.track("a.b", {"k": 1}, user_id=GUEST)
        before = len(list(iter_events()))

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))
        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert len(list(iter_events())) == before

    def test_a_redelivered_merge_moves_nothing_the_second_time(self):
        services.track("a.b", {"k": 1}, user_id=GUEST)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))
        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        rows = list(iter_events(filters={"user_hash": hash_user_id(SURVIVOR)}))
        assert len(rows) == 1

    def test_a_third_partys_rows_are_untouched(self):
        other = str(uuid.uuid4())
        services.track("a.b", {"k": 1}, user_id=GUEST)
        services.track("a.b", {"k": 9}, user_id=other)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        kept = list(iter_events(filters={"user_hash": hash_user_id(other)}))
        assert [e["props"]["k"] for e in kept] == [9]

    def test_the_rest_of_the_row_survives_the_re_key(self):
        """A re-key moves the subject key and nothing else."""
        services.track("a.b", {"k": 1}, user_id=GUEST, anon_id="anon-1",
                       session_id="sess-1")

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        (row,) = list(iter_events(filters={"user_hash": hash_user_id(SURVIVOR)}))
        assert row["anon_id"] == "anon-1"
        assert row["session_id"] == "sess-1"
        assert row["name"] == "a.b"
        assert row["props"] == {"k": 1}

    def test_a_self_merge_moves_nothing(self):
        services.track("a.b", {"k": 1}, user_id=GUEST)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=GUEST))

        rows = list(iter_events(filters={"user_hash": hash_user_id(GUEST)}))
        assert len(rows) == 1

    def test_the_re_key_appends_no_row_of_its_own(self):
        """Silent by contract — a merge is not an analytics event."""
        services.track("a.b", {"k": 1}, user_id=GUEST)
        before = len(list(iter_events()))

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert len(list(iter_events())) == before


@pytest.mark.django_db
class TestAStoreThatCannotReKey:
    """A deployment that routed the stream to a backend without ``rekey``.

    The funnel half is still worth doing, so it runs; the stream half is
    reported at ERROR rather than raised. Raising would be a poison pill the
    bus replays forever over a condition that is a storage choice, not a
    transient fault.
    """

    @pytest.fixture(autouse=True)
    def _declared(self, settings):
        settings.STAPEL_ANALYTICS = {"EVENTS": {"a.b": {"description": "x"}}}

    def test_the_funnel_still_moves_and_the_gap_is_logged(self, caplog):
        from stapel_core.eventstore import RekeyUnsupported

        mine = _funnel(GUEST, "guest-funnel-norekey")

        def _refuse(**kwargs):
            raise RekeyUnsupported("routed backend cannot re-key")

        import stapel_analytics.store as store_module

        original = store_module.rekey_subject
        store_module.rekey_subject = _refuse
        try:
            with caplog.at_level("ERROR"):
                handle_user_merged(
                    _event(from_user_id=GUEST, into_user_id=SURVIVOR)
                )
        finally:
            store_module.rekey_subject = original

        assert str(Funnel.objects.get(pk=mine.pk).owner_id) == SURVIVOR
        assert "cannot re-key" in caplog.text

    def test_it_does_not_raise_into_the_bus(self):
        from stapel_core.eventstore import RekeyUnsupported

        def _refuse(**kwargs):
            raise RekeyUnsupported("routed backend cannot re-key")

        import stapel_analytics.store as store_module

        original = store_module.rekey_subject
        store_module.rekey_subject = _refuse
        try:
            handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))
        finally:
            store_module.rekey_subject = original
