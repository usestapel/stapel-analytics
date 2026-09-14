"""The attribution cookie: decode, first touch, capture, expiry, erasure.

The cookie is written by somebody else's marketing site, so the decode is
tested the way a stranger's string deserves — padded and unpadded, truncated,
wrong shape, wrong platform, wrong timestamp — and every one of those has to
end in "ignored and counted", never in an exception a request could see.
"""
import base64
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.utils import timezone

from stapel_analytics import attribution
from stapel_analytics.middleware import AttributionCookieMiddleware
from stapel_analytics.models import ConversionUpload, UserAttribution

#: The envelope the host's marketing site writes: base64url of
#: {"id", "src", "ts"}, unpadded.
HOST_FIELDS = {"id": "id", "type": "src", "ts": "ts"}
COOKIE = "ironmemo_attr"


def envelope(payload: dict, *, pad: bool = False) -> str:
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return raw if pad else raw.rstrip("=")


def claim(click_id="EAIaIQclick", src="gclid", ts=None, **extra):
    return {
        "id": click_id,
        "src": src,
        "ts": int((ts or timezone.now()).timestamp()),
        **extra,
    }


@pytest.fixture
def cookie_settings(settings):
    """Configure the cookie the way the host does, and hand back the tweaker."""

    def _configure(**overrides):
        settings.STAPEL_ANALYTICS = {
            **getattr(settings, "STAPEL_ANALYTICS", {}),
            "ATTRIBUTION_COOKIE": {
                "NAME": COOKIE,
                "FIELDS": HOST_FIELDS,
                **overrides,
            },
        }

    _configure()
    return _configure


class TestDecode:
    def test_the_shipped_envelope_decodes(self, cookie_settings):
        now = timezone.now().replace(microsecond=0)
        decoded = attribution.decode(envelope(claim(ts=now)))
        assert decoded["click_id"] == "EAIaIQclick"
        assert decoded["click_id_type"] == "gclid"
        assert decoded["clicked_at"] == now

    @pytest.mark.parametrize("click_id", ["a", "ab", "abc", "abcd"])
    def test_every_padding_remainder_decodes(self, cookie_settings, click_id):
        """A base64url value whose length is not a multiple of four is the
        normal case: the padding is restored here, not demanded."""
        value = envelope(claim(click_id=click_id))
        assert len(value) % 4 in (0, 2, 3)
        assert attribution.decode(value)["click_id"] == click_id

    def test_padded_values_decode_too(self, cookie_settings):
        assert attribution.decode(envelope(claim(), pad=True)) is not None

    def test_unknown_fields_are_ignored(self, cookie_settings):
        decoded = attribution.decode(envelope(claim(v=2, utm="brand")))
        assert decoded["click_id"] == "EAIaIQclick"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not base64 at all!!",
            base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
            base64.urlsafe_b64encode(b'["a list"]').decode().rstrip("="),
        ],
    )
    def test_a_broken_envelope_is_ignored(self, cookie_settings, value):
        assert attribution.decode(value) is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"src": "gclid", "ts": 1789000000},              # no identifier
            {"id": "", "src": "gclid", "ts": 1789000000},    # blank identifier
            {"id": "x", "ts": 1789000000},                   # no platform
            {"id": "x", "src": "unknown", "ts": 1789000000},  # a platform
            {"id": "x", "src": "gclid"},                     # no click time
            {"id": "x", "src": "gclid", "ts": "soon"},       # unusable time
            {"id": "x", "src": "gclid", "ts": 1789000000000},  # milliseconds
            {"id": "x" * 600, "src": "gclid", "ts": 1789000000},  # unbounded
        ],
    )
    def test_a_record_nothing_could_report_is_ignored(
        self, cookie_settings, payload
    ):
        assert attribution.decode(envelope(payload)) is None

    def test_a_format_this_release_cannot_read_is_ignored(self, cookie_settings):
        cookie_settings(FORMAT="jwt")
        assert attribution.decode(envelope(claim())) is None

    def test_malformed_is_counted_never_raised(self, cookie_settings):
        with patch("stapel_core.observability.metrics.counter") as counter:
            assert attribution.decode("###") is None
        names = [call.args[0] for call in counter.call_args_list]
        assert attribution.METRIC_MALFORMED in names

    def test_the_fields_map_is_merged_not_replaced(self, settings):
        """A host that names only the cookie keeps the shipped envelope."""
        settings.STAPEL_ANALYTICS = {
            **getattr(settings, "STAPEL_ANALYTICS", {}),
            "ATTRIBUTION_COOKIE": {"NAME": COOKIE},
        }
        resolved = attribution.cookie_settings()
        assert resolved["FIELDS"] == {"id": "id", "type": "type", "ts": "ts"}
        assert resolved["FORMAT"] == attribution.FORMAT_BASE64URL_JSON
        assert resolved["FIRST_TOUCH"] is False


@pytest.mark.django_db
class TestRecord:
    def test_it_stores_the_click_and_its_time(self, cookie_settings, user):
        clicked = timezone.now().replace(microsecond=0) - timedelta(days=3)
        row, created = attribution.record(
            user, attribution.decode(envelope(claim(ts=clicked)))
        )
        assert created is True
        assert row.click_id == "EAIaIQclick"
        assert row.click_id_type == "gclid"
        assert row.clicked_at == clicked
        assert row.source == "cookie"
        assert row.expired is False
        assert row.captured_at is not None

    def test_a_newer_click_replaces_the_stored_one(self, cookie_settings, user):
        """Last touch. The click Google has to be told about is the one that
        made the person pay, and that is the newest, not the first."""
        first = timezone.now() - timedelta(days=10)
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="first", ts=first)))
        )
        row, written = attribution.record(
            user, attribution.decode(envelope(claim(click_id="second")))
        )
        assert written is True
        assert row.click_id == "second"
        assert UserAttribution.objects.count() == 1
        assert attribution.attribution_for(user).click_id == "second"

    def test_an_older_click_is_ignored(self, cookie_settings, user):
        stored = timezone.now() - timedelta(days=2)
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="stored", ts=stored)))
        )
        row, written = attribution.record(
            user,
            attribution.decode(
                envelope(claim(click_id="older", ts=stored - timedelta(days=1)))
            ),
        )
        assert written is False
        assert row.click_id == "stored"

    def test_an_equally_old_click_is_ignored(self, cookie_settings, user):
        """Not `>=`: a repeat of the SAME click is the ordinary case — the
        cookie rides every request — and rewriting the row on each one would
        be a write per request for no new fact."""
        stored = timezone.now().replace(microsecond=0) - timedelta(days=2)
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="stored", ts=stored)))
        )
        row, written = attribution.record(
            user, attribution.decode(envelope(claim(click_id="twin", ts=stored)))
        )
        assert written is False
        assert row.click_id == "stored"

    def test_first_touch_can_still_be_asked_for(self, cookie_settings, user):
        """The flag survives the flip: a host that wants the first click is
        one setting away from it."""
        cookie_settings(FIRST_TOUCH=True)
        first = timezone.now() - timedelta(days=10)
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="first", ts=first)))
        )
        row, written = attribution.record(
            user, attribution.decode(envelope(claim(click_id="second")))
        )
        assert written is False
        assert row.click_id == "first"

    def test_a_record_with_no_click_time_yields_to_a_click_after_its_capture(
        self, cookie_settings, user
    ):
        """A stored row whose click time is unknown (the explicit door often
        has only `captured_at`) is beaten by the one comparison that holds
        without it: a click that happened AFTER that row was captured."""
        captured = timezone.now() - timedelta(days=5)
        UserAttribution.objects.create(
            user_id=user.pk,
            click_id="timeless",
            click_id_type="gclid",
            clicked_at=None,
            captured_at=captured,
            source="signup",
        )
        attribution.record(
            user,
            attribution.decode(
                envelope(claim(click_id="before", ts=captured - timedelta(days=1)))
            ),
        )
        assert attribution.attribution_for(user).click_id == "timeless"
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="after")))
        )
        assert attribution.attribution_for(user).click_id == "after"

    def test_a_click_past_the_window_is_stored_as_expired(
        self, cookie_settings, user
    ):
        clicked = timezone.now() - timedelta(days=120)
        row, _created = attribution.record(
            user, attribution.decode(envelope(claim(ts=clicked)))
        )
        assert row.expired is True

    def test_a_yandex_click_is_stored_like_any_other(self, cookie_settings, user):
        row, _created = attribution.record(
            user, attribution.decode(envelope(claim(src="yclid")))
        )
        assert row.click_id_type == "yclid"

    def test_the_accessor_answers_for_an_id_as_well_as_an_object(
        self, cookie_settings, user
    ):
        attribution.record(user, attribution.decode(envelope(claim())))
        assert attribution.attribution_for(user.pk) is not None
        assert attribution.attribution_for(str(user.pk)) is not None


@pytest.mark.django_db
class TestMiddleware:
    def _request(self, user, *, cookie=None, path="/", query=""):
        request = RequestFactory().get(path + (f"?{query}" if query else ""))
        request.user = user
        if cookie is not None:
            request.COOKIES[COOKIE] = cookie
        return request

    def _run(self, request):
        calls = []

        def view(req):
            calls.append(req)
            return "response"

        assert AttributionCookieMiddleware(view)(request) == "response"
        assert calls == [request]

    def test_an_authenticated_request_is_captured(self, cookie_settings, user):
        self._run(self._request(user, cookie=envelope(claim())))
        assert attribution.attribution_for(user).click_id == "EAIaIQclick"

    def test_an_anonymous_account_is_an_account(self, cookie_settings, db):
        """A guest enrolment is an account: it can pay, so it is attributed."""
        from django.contrib.auth import get_user_model

        guest = get_user_model().objects.create(
            username="guest-7f3", email="", is_active=True
        )
        assert guest.is_authenticated
        self._run(self._request(guest, cookie=envelope(claim())))
        assert attribution.attribution_for(guest) is not None

    def test_a_visitor_with_no_account_is_not_captured(self, cookie_settings):
        self._run(self._request(AnonymousUser(), cookie=envelope(claim())))
        assert UserAttribution.objects.count() == 0

    def test_no_cookie_no_row(self, cookie_settings, user):
        self._run(self._request(user))
        assert UserAttribution.objects.count() == 0

    def test_capture_off_by_default(self, settings, user):
        settings.STAPEL_ANALYTICS = {
            **getattr(settings, "STAPEL_ANALYTICS", {}),
            "ATTRIBUTION_COOKIE": {},
        }
        self._run(self._request(user, cookie=envelope(claim())))
        assert UserAttribution.objects.count() == 0

    def test_the_url_attribution_stands_the_cookie_down_on_that_request(
        self, cookie_settings, user
    ):
        """The explicit write is in flight on this very request; there is
        nothing stored yet to compare it against, so the cookie waits."""
        self._run(
            self._request(
                user, cookie=envelope(claim()), query="click_id=URL&click_id_type=gclid"
            )
        )
        assert UserAttribution.objects.count() == 0

    def test_the_cookie_lands_on_the_next_request_without_the_parameter(
        self, cookie_settings, user
    ):
        """Standing down costs one request, not the capture: the middleware
        runs the rule again on the next one."""
        self._run(
            self._request(
                user, cookie=envelope(claim()), query="click_id=URL&click_id_type=gclid"
            )
        )
        self._run(self._request(user, cookie=envelope(claim())))
        assert attribution.attribution_for(user).click_id == "EAIaIQclick"

    def test_a_newer_click_overwrites_a_stored_explicit_attribution(
        self, cookie_settings, user
    ):
        """Which DOOR the stored record came through no longer decides it —
        which CLICK is newer does. Google needs the click that made the
        person pay."""
        UserAttribution.objects.create(
            user_id=user.pk,
            click_id="from-the-url",
            click_id_type="gclid",
            clicked_at=timezone.now() - timedelta(days=30),
            captured_at=timezone.now() - timedelta(days=30),
            source="signup",
        )
        self._run(self._request(user, cookie=envelope(claim())))
        assert attribution.attribution_for(user).click_id == "EAIaIQclick"

    def test_an_older_click_leaves_a_stored_explicit_attribution_alone(
        self, cookie_settings, user
    ):
        UserAttribution.objects.create(
            user_id=user.pk,
            click_id="from-the-url",
            click_id_type="gclid",
            clicked_at=timezone.now() - timedelta(hours=1),
            captured_at=timezone.now() - timedelta(hours=1),
            source="signup",
        )
        self._run(
            self._request(
                user,
                cookie=envelope(claim(ts=timezone.now() - timedelta(days=9))),
            )
        )
        assert attribution.attribution_for(user).click_id == "from-the-url"

    def test_the_rule_runs_on_every_request_not_only_the_first(
        self, cookie_settings, user
    ):
        """0.6.0 captured once and never looked again. A person who clicks a
        second ad while already signed in must move the account onto it."""
        first = timezone.now() - timedelta(days=20)
        self._run(
            self._request(
                user, cookie=envelope(claim(click_id="first", ts=first))
            )
        )
        self._run(
            self._request(user, cookie=envelope(claim(click_id="second")))
        )
        assert attribution.attribution_for(user).click_id == "second"
        assert UserAttribution.objects.count() == 1

    def test_a_repeat_of_the_same_cookie_writes_nothing(
        self, cookie_settings, user
    ):
        """Cheap is the whole reason this can run on every request: the
        comparison is a read, and an unchanged cookie never reaches a write."""
        cookie = envelope(claim(ts=timezone.now() - timedelta(days=1)))
        self._run(self._request(user, cookie=cookie))
        stored = attribution.attribution_for(user)
        for _ in range(3):
            self._run(self._request(user, cookie=cookie))
        again = attribution.attribution_for(user)
        assert again.updated_at == stored.updated_at
        assert again.click_id == stored.click_id

    def test_a_malformed_cookie_is_counted_and_the_request_stands(
        self, cookie_settings, user
    ):
        with patch("stapel_core.observability.metrics.counter") as counter:
            self._run(self._request(user, cookie="%%%broken%%%"))
        assert UserAttribution.objects.count() == 0
        assert attribution.METRIC_MALFORMED in [
            call.args[0] for call in counter.call_args_list
        ]

    def test_a_failing_capture_never_costs_a_response(
        self, cookie_settings, user
    ):
        with patch(
            "stapel_analytics.attribution.capture", side_effect=RuntimeError("db")
        ):
            self._run(self._request(user, cookie=envelope(claim())))


@pytest.mark.django_db
class TestHistory:
    """Last touch decides the upload; the history is what keeps reporting
    able to answer "and which click STARTED this account"."""

    def _click(self, user, click_id, days_ago):
        attribution.record(
            user,
            attribution.decode(
                envelope(
                    claim(
                        click_id=click_id,
                        ts=timezone.now() - timedelta(days=days_ago),
                    )
                )
            ),
        )

    def test_the_superseded_click_is_kept(self, cookie_settings, user):
        self._click(user, "first", 30)
        self._click(user, "second", 1)
        row = attribution.attribution_for(user)
        assert row.click_id == "second"
        assert [entry["click_id"] for entry in row.history] == ["first"]
        assert row.history[0]["click_id_type"] == "gclid"
        assert row.history[0]["clicked_at"] is not None
        assert row.history[0]["source"] == "cookie"

    def test_an_ignored_click_is_not_history(self, cookie_settings, user):
        """History is the record of what this account WAS attributed to, not
        a log of every cookie that rode in."""
        self._click(user, "stored", 1)
        self._click(user, "older", 30)
        assert attribution.attribution_for(user).history == []

    def test_the_history_is_capped(self, cookie_settings, user):
        for index in range(attribution.HISTORY_LIMIT + 5):
            self._click(user, f"click-{index}", 100 - index)
        row = attribution.attribution_for(user)
        assert len(row.history) == attribution.HISTORY_LIMIT

    def test_the_first_touch_survives_the_cap(self, cookie_settings, user):
        """A cap that evicted the oldest entry would throw away the one thing
        the history exists for."""
        for index in range(attribution.HISTORY_LIMIT + 5):
            self._click(user, f"click-{index}", 100 - index)
        row = attribution.attribution_for(user)
        assert row.history[0]["click_id"] == "click-0"
        assert row.history[-1]["click_id"] == "click-13"
        assert row.click_id == "click-14"

    def test_the_record_carries_its_history(self, cookie_settings, user):
        self._click(user, "first", 30)
        self._click(user, "second", 1)
        as_dict = attribution.attribution_as_dict(
            attribution.attribution_for(user)
        )
        assert as_dict["click_id"] == "second"
        assert [entry["click_id"] for entry in as_dict["history"]] == ["first"]


@pytest.mark.django_db
class TestTheAccountLifeCycle:
    def test_an_upgraded_anonymous_account_keeps_its_row(
        self, cookie_settings, db
    ):
        """A guest that signs up in place keeps its user id, so the row it
        was attributed under is the same row — nothing to move, and nothing
        may be lost."""
        from django.contrib.auth import get_user_model

        guest = get_user_model().objects.create(username="guest-9a2", email="")
        attribution.record(
            guest,
            attribution.decode(
                envelope(
                    claim(click_id="as-a-guest", ts=timezone.now() - timedelta(days=9))
                )
            ),
        )
        guest.email = "signed-up@example.com"
        guest.username = "signed-up"
        guest.save()
        row = attribution.attribution_for(guest)
        assert row is not None
        assert row.click_id == "as-a-guest"
        # And the rule keeps running on the upgraded account.
        attribution.record(
            guest, attribution.decode(envelope(claim(click_id="after-signup")))
        )
        row = attribution.attribution_for(guest)
        assert row.click_id == "after-signup"
        assert [entry["click_id"] for entry in row.history] == ["as-a-guest"]

    def _row(self, user_id, click_id, days_ago, *, history=None):
        moment = timezone.now() - timedelta(days=days_ago)
        return UserAttribution.objects.create(
            user_id=user_id,
            click_id=click_id,
            click_id_type="gclid",
            clicked_at=moment,
            captured_at=moment,
            source="cookie",
            history=history or [],
        )

    def test_a_merged_guest_hands_its_row_to_a_survivor_with_none(
        self, cookie_settings, user, other_user
    ):
        self._row(user.pk, "guest-click", 5)
        moved = attribution.merge_accounts(user.pk, other_user.pk)
        assert moved == 1
        assert attribution.attribution_for(user) is None
        assert attribution.attribution_for(other_user).click_id == "guest-click"

    def test_a_newer_guest_click_becomes_the_survivors(
        self, cookie_settings, user, other_user
    ):
        self._row(user.pk, "guest-click", 1)
        self._row(other_user.pk, "survivor-click", 40)
        attribution.merge_accounts(user.pk, other_user.pk)
        survivor = attribution.attribution_for(other_user)
        assert survivor.click_id == "guest-click"
        assert [entry["click_id"] for entry in survivor.history] == [
            "survivor-click"
        ]
        assert UserAttribution.objects.count() == 1

    def test_an_older_guest_click_only_joins_the_history(
        self, cookie_settings, user, other_user
    ):
        self._row(user.pk, "guest-click", 40)
        self._row(other_user.pk, "survivor-click", 1)
        attribution.merge_accounts(user.pk, other_user.pk)
        survivor = attribution.attribution_for(other_user)
        assert survivor.click_id == "survivor-click"
        assert [entry["click_id"] for entry in survivor.history] == ["guest-click"]

    def test_the_guests_own_history_travels_with_it(
        self, cookie_settings, user, other_user
    ):
        older = (timezone.now() - timedelta(days=90)).isoformat()
        self._row(
            user.pk,
            "guest-click",
            40,
            history=[
                {
                    "click_id": "guest-first-touch",
                    "click_id_type": "gclid",
                    "clicked_at": older,
                    "captured_at": older,
                    "source": "cookie",
                    "expired": False,
                }
            ],
        )
        self._row(other_user.pk, "survivor-click", 1)
        attribution.merge_accounts(user.pk, other_user.pk)
        survivor = attribution.attribution_for(other_user)
        assert [entry["click_id"] for entry in survivor.history] == [
            "guest-first-touch",
            "guest-click",
        ]

    def test_the_merge_is_idempotent(self, cookie_settings, user, other_user):
        self._row(user.pk, "guest-click", 1)
        self._row(other_user.pk, "survivor-click", 40)
        assert attribution.merge_accounts(user.pk, other_user.pk) == 1
        assert attribution.merge_accounts(user.pk, other_user.pk) == 0
        survivor = attribution.attribution_for(other_user)
        assert survivor.click_id == "guest-click"
        assert [entry["click_id"] for entry in survivor.history] == [
            "survivor-click"
        ]

    def test_a_merge_with_nothing_to_carry_is_a_no_op(
        self, cookie_settings, user, other_user
    ):
        assert attribution.merge_accounts(user.pk, other_user.pk) == 0

    def test_the_user_merged_subscriber_carries_the_attribution(
        self, cookie_settings, user, other_user
    ):
        """The seam: a merge arriving on the bus must move the marketing
        record too, or the survivor pays and the click is on a row nobody
        will ever read."""
        from stapel_analytics.actions import handle_user_merged

        self._row(user.pk, "guest-click", 1)
        self._row(other_user.pk, "survivor-click", 40)
        handle_user_merged(
            SimpleNamespace(
                event_id="evt-merge",
                payload={
                    "from_user_id": str(user.pk),
                    "into_user_id": str(other_user.pk),
                },
            )
        )
        survivor = attribution.attribution_for(other_user)
        assert survivor.click_id == "guest-click"
        assert [entry["click_id"] for entry in survivor.history] == [
            "survivor-click"
        ]


@pytest.mark.django_db
class TestTheConversionPath:
    """What the stored click is for: the 90-day rule becomes the real one."""

    def test_the_conversion_is_enqueued_against_the_last_click(
        self, cookie_settings, user
    ):
        from stapel_analytics import conversions

        attribution.record(
            user,
            attribution.decode(
                envelope(
                    claim(click_id="first", ts=timezone.now() - timedelta(days=30))
                )
            ),
        )
        attribution.record(
            user,
            attribution.decode(
                envelope(
                    claim(click_id="paid-for-it", ts=timezone.now() - timedelta(days=2))
                )
            ),
        )
        upload, created = conversions.enqueue_for_user(
            user,
            conversion_action="customers/1/conversionActions/2",
            conversion_at=timezone.now(),
            value=Decimal("19.00"),
            currency="usd",
        )
        assert created is True
        assert upload.click_id == "paid-for-it"
        assert upload.click_id_type == "gclid"
        assert upload.clicked_at == attribution.attribution_for(user).clicked_at
        assert upload.currency == "USD"

    def test_an_account_with_no_click_enqueues_nothing(
        self, cookie_settings, user
    ):
        from stapel_analytics import conversions

        assert (
            conversions.enqueue_for_user(
                user,
                conversion_action="customers/1/conversionActions/2",
                conversion_at=timezone.now(),
            )
            is None
        )
        assert ConversionUpload.objects.count() == 0

    def test_a_click_the_google_feed_cannot_carry_enqueues_nothing(
        self, cookie_settings, user
    ):
        from stapel_analytics import conversions

        attribution.record(
            user, attribution.decode(envelope(claim(src="yclid")))
        )
        assert (
            conversions.enqueue_for_user(
                user,
                conversion_action="customers/1/conversionActions/2",
                conversion_at=timezone.now(),
            )
            is None
        )
        assert ConversionUpload.objects.count() == 0

    def test_the_stored_click_time_drives_the_window(self, cookie_settings, user):
        clicked = timezone.now() - timedelta(days=100)
        row, _created = attribution.record(
            user, attribution.decode(envelope(claim(ts=clicked)))
        )
        from stapel_analytics import conversions

        upload, _made = conversions.enqueue(
            click_id=row.click_id,
            click_id_type=row.click_id_type,
            conversion_action="customers/1/conversionActions/2",
            conversion_at=timezone.now(),
            clicked_at=row.clicked_at,
        )
        # Without clicked_at this conversion looks fresh; with it, it is the
        # refusal Google would have made, made locally instead.
        assert conversions.stale_verdict(upload) == conversions.REASON_WINDOW

    def test_the_google_feed_excludes_a_yandex_click(self, cookie_settings, settings):
        from stapel_analytics import feed

        settings.STAPEL_ANALYTICS = {
            **getattr(settings, "STAPEL_ANALYTICS", {}),
            "CONVERSION_FEED_TOKEN": "feed-token",
            "CONVERSION_FEED_CONVERSION_NAME": "Paid account",
        }
        now = timezone.now()
        for click_id, kind in (("g-click", "gclid"), ("y-click", "yclid")):
            ConversionUpload.objects.create(
                click_id=click_id,
                click_id_type=kind,
                conversion_action="customers/1/conversionActions/2",
                conversion_at=now,
                clicked_at=now - timedelta(days=1),
            )
        served = [row.click_id for row in feed.feed_rows(now=now)]
        assert served == ["g-click"]
        assert "y-click" not in feed.render_csv(feed.feed_rows(now=now))


@pytest.mark.django_db
class TestErasure:
    def test_erasing_an_account_deletes_its_attribution(
        self, cookie_settings, user
    ):
        from stapel_analytics.erasure import erase_account

        attribution.record(user, attribution.decode(envelope(claim())))
        counts = erase_account(user.pk)
        assert counts["attribution"] == 1
        assert attribution.attribution_for(user) is None
        # Idempotent: a second erasure finds nothing and says so.
        assert erase_account(user.pk)["attribution"] == 0

    def test_the_export_carries_the_click(self, cookie_settings, user):
        from stapel_analytics.erasure import export_account

        attribution.record(user, attribution.decode(envelope(claim())))
        exported = export_account(user.pk)["attribution"]
        assert exported["click_id"] == "EAIaIQclick"
        assert exported["click_id_type"] == "gclid"


class TestTheMountCheck:
    def test_a_named_cookie_nobody_reads_warns(self, settings, cookie_settings):
        from stapel_analytics.checks import check_attribution_middleware

        settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
        warnings = check_attribution_middleware(None)
        assert [warning.id for warning in warnings] == ["analytics.W013"]

    def test_mounted_is_silent(self, settings, cookie_settings):
        from stapel_analytics.checks import (
            ATTRIBUTION_MIDDLEWARE,
            check_attribution_middleware,
        )

        settings.MIDDLEWARE = [ATTRIBUTION_MIDDLEWARE]
        assert check_attribution_middleware(None) == []

    def test_no_cookie_no_warning(self, settings):
        from stapel_analytics.checks import check_attribution_middleware

        settings.STAPEL_ANALYTICS = {
            **getattr(settings, "STAPEL_ANALYTICS", {}),
            "ATTRIBUTION_COOKIE": {},
        }
        settings.MIDDLEWARE = []
        assert check_attribution_middleware(None) == []
