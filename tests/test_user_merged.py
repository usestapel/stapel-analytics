"""A merge is not a delete — the other half of an account's life cycle.

stapel-auth folds an anonymous guest into an existing account on sign-in and
then deletes the guest row. This module answered ``user.deleted`` from its
first release and said nothing about ``user.merged``, and silence there is
not neutrality — it is a wrong answer given quietly.

What this module can carry over, it carries: ``Funnel.owner_id``. What it
cannot, it says out loud, and the last class here pins the gap so that
closing it later is a deliberate edit rather than a surprise.
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
class TestTheEventStreamIsNotReKeyed:
    """The documented gap, pinned so closing it is a deliberate edit.

    The pseudonymisation is not what stops a re-key: ``hash_user_id`` is a
    forward hash and the payload carries both raw ids, so both hashes are
    computable here. What stops it is that ``stapel_core.eventstore`` has no
    update — a re-key would be read, append-under-the-new-hash, purge-the-old,
    three calls with no transaction spanning them, driven by an at-least-once
    handler. Interrupted between the append and the purge it counts one
    person's history twice, in a store whose whole job is arithmetic.

    So the guest's rows keep their own ``user_hash``. If a future release
    adds an atomic subject re-key to core, this class is what has to change,
    and changing it is the moment somebody re-reads the reasoning.
    """

    @pytest.fixture(autouse=True)
    def _declared(self, settings):
        settings.STAPEL_ANALYTICS = {"EVENTS": {"a.b": {"description": "x"}}}

    def test_the_guests_rows_keep_their_own_subject_key(self):
        services.track("a.b", {"k": 1}, user_id=GUEST)
        services.track("a.b", {"k": 2}, user_id=SURVIVOR)

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        guest_hash = hash_user_id(GUEST)
        survivor_hash = hash_user_id(SURVIVOR)
        assert len(list(iter_events(filters={"user_hash": guest_hash}))) == 1
        assert len(list(iter_events(filters={"user_hash": survivor_hash}))) == 1

    def test_no_row_is_duplicated_by_the_merge(self):
        """The failure the missing re-key exists to avoid, asserted directly."""
        services.track("a.b", {"k": 1}, user_id=GUEST)
        before = len(list(iter_events()))

        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))
        handle_user_merged(_event(from_user_id=GUEST, into_user_id=SURVIVOR))

        assert len(list(iter_events())) == before
