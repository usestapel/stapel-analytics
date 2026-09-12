"""The attribution cookie: decode, first touch, capture, expiry, erasure.

The cookie is written by somebody else's marketing site, so the decode is
tested the way a stranger's string deserves — padded and unpadded, truncated,
wrong shape, wrong platform, wrong timestamp — and every one of those has to
end in "ignored and counted", never in an exception a request could see.
"""
import base64
import json
from datetime import timedelta
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
        assert resolved["FIRST_TOUCH"] is True


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

    def test_first_touch_never_overwrites(self, cookie_settings, user):
        first = timezone.now() - timedelta(days=10)
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="first", ts=first)))
        )
        row, created = attribution.record(
            user, attribution.decode(envelope(claim(click_id="second")))
        )
        assert created is False
        assert row.click_id == "first"
        assert UserAttribution.objects.count() == 1

    def test_last_touch_takes_a_newer_click_only(self, cookie_settings, user):
        cookie_settings(FIRST_TOUCH=False)
        older = timezone.now() - timedelta(days=10)
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="first", ts=older)))
        )
        attribution.record(
            user,
            attribution.decode(
                envelope(claim(click_id="older", ts=older - timedelta(days=1)))
            ),
        )
        assert attribution.attribution_for(user).click_id == "first"
        attribution.record(
            user, attribution.decode(envelope(claim(click_id="newer")))
        )
        assert attribution.attribution_for(user).click_id == "newer"
        assert UserAttribution.objects.count() == 1

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

    def test_the_url_attribution_wins_over_the_cookie(
        self, cookie_settings, user
    ):
        """The frontend passing click_id on this very request is the explicit
        door; the cookie stands down rather than racing it."""
        self._run(
            self._request(
                user, cookie=envelope(claim()), query="click_id=URL&click_id_type=gclid"
            )
        )
        assert UserAttribution.objects.count() == 0

    def test_a_stored_attribution_is_never_overwritten(
        self, cookie_settings, user
    ):
        UserAttribution.objects.create(
            user_id=user.pk,
            click_id="from-the-url",
            click_id_type="gclid",
            clicked_at=timezone.now() - timedelta(days=1),
            captured_at=timezone.now() - timedelta(days=1),
            source="signup",
        )
        self._run(self._request(user, cookie=envelope(claim())))
        assert attribution.attribution_for(user).click_id == "from-the-url"

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
class TestTheConversionPath:
    """What the stored click is for: the 90-day rule becomes the real one."""

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
