"""The conversion feed: the file, the window, the token and the receipt.

What is asserted here is the RESPONSE BODY, parsed as CSV, not that a view
returned 200. The whole contract of this endpoint is a file layout somebody
else's importer reads without ever talking to us: a header spelled
differently, a click id in the wrong column or a timestamp without an offset
is a fetch that succeeds, a file that parses and zero conversions imported
— the exact failure that looks healthy from here.
"""
import csv
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from io import StringIO as _StringIO

from stapel_analytics import conversions, feed, tasks
from stapel_analytics.models import ConversionFeedFetch, ConversionUpload

FEED = "/analytics/api/v1/conversions/google-ads.csv"
TOKEN = "feed-token-0123456789"
NAME = "Paid registration (offline import)"

ACTION = "customers/1234567890/conversionActions/42"


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.STAPEL_ANALYTICS = {
        "CONVERSION_FEED_TOKEN": TOKEN,
        "CONVERSION_FEED_CONVERSION_NAME": NAME,
    }
    return settings


def outbox_row(**overrides):
    now = timezone.now()
    values = {
        "click_id": "Cj0KCQjw-click",
        "click_id_type": "gclid",
        "conversion_action": ACTION,
        "conversion_at": now - timedelta(days=1),
        "clicked_at": now - timedelta(days=3),
        "value": Decimal("49.0000"),
        "currency": "EUR",
    }
    values.update(overrides)
    return ConversionUpload.objects.create(**values)


def parse(body: str):
    return list(csv.DictReader(StringIO(body)))


def fetch(client, token=TOKEN, **kwargs):
    if token is None:
        return client.get(FEED, **kwargs)
    return client.get(FEED, HTTP_AUTHORIZATION=f"Bearer {token}", **kwargs)


# ── The layout ───────────────────────────────────────────────────────


@pytest.mark.django_db
class TestLayout:
    def test_the_header_line_is_the_template_s_columns(self, api_client):
        body = fetch(api_client).content.decode()
        assert body.splitlines()[0] == (
            "Google Click ID,GBRAID,WBRAID,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency"
        )

    def test_an_empty_outbox_still_answers_the_headers(self, api_client):
        """A header-only file says "the feed works and there is nothing to
        report". An empty body says nothing at all."""
        response = fetch(api_client)
        assert response.status_code == 200
        assert parse(response.content.decode()) == []
        assert response.content.decode().startswith("Google Click ID,")

    def test_the_content_type_is_csv(self, api_client):
        assert fetch(api_client)["Content-Type"] == "text/csv; charset=utf-8"

    def test_a_row_carries_the_click_id_name_time_value_and_currency(
        self, api_client
    ):
        row = outbox_row()
        served = parse(fetch(api_client).content.decode())
        assert len(served) == 1
        assert served[0]["Google Click ID"] == row.click_id
        assert served[0]["Conversion Name"] == NAME
        assert served[0]["Conversion Value"] == "49"
        assert served[0]["Conversion Currency"] == "EUR"

    def test_a_conversion_without_a_value_carries_neither_value_nor_currency(
        self, api_client
    ):
        """A lead is a conversion that counts and carries no money — an
        empty cell, not a zero somebody's bidding would learn from."""
        outbox_row(value=None, currency="")
        served = parse(fetch(api_client).content.decode())
        assert served[0]["Conversion Value"] == ""
        assert served[0]["Conversion Currency"] == ""

    def test_rows_come_oldest_conversion_first(self, api_client):
        now = timezone.now()
        outbox_row(click_id="second", conversion_at=now - timedelta(days=1))
        outbox_row(click_id="first", conversion_at=now - timedelta(days=5))
        served = parse(fetch(api_client).content.decode())
        assert [r["Google Click ID"] for r in served] == ["first", "second"]


@pytest.mark.django_db
class TestClickIdColumns:
    def test_every_click_id_kind_has_its_own_column(self):
        """Google models the three as distinct fields, and so does the file.
        One assertion for the whole mapping, so a fourth kind cannot be
        added to the model without landing here."""
        assert feed.CLICK_ID_COLUMNS == {
            "gclid": "Google Click ID",
            "gbraid": "GBRAID",
            "wbraid": "WBRAID",
        }
        assert set(feed.CLICK_ID_COLUMNS) == set(conversions.CLICK_ID_FIELDS)

    @pytest.mark.parametrize(
        "kind,column",
        [
            ("gclid", "Google Click ID"),
            ("gbraid", "GBRAID"),
            ("wbraid", "WBRAID"),
        ],
    )
    def test_the_id_lands_in_its_column_and_the_others_stay_empty(
        self, api_client, kind, column
    ):
        outbox_row(click_id=f"{kind}-value", click_id_type=kind)
        served = parse(fetch(api_client).content.decode())[0]
        assert served[column] == f"{kind}-value"
        empty = set(feed.CLICK_ID_COLUMNS.values()) - {column}
        assert [served[name] for name in empty] == ["", ""]


@pytest.mark.django_db
class TestConversionTime:
    def test_the_timestamp_carries_an_offset(self, api_client):
        outbox_row()
        served = parse(fetch(api_client).content.decode())[0]
        assert re_time(served["Conversion Time"])

    def test_the_offset_is_the_deployment_s_own_not_a_guessed_z(
        self, api_client, settings
    ):
        """A conversion time without an offset is a number whose meaning
        depends on which account reads it — the import refuses it."""
        settings.USE_TZ = True
        settings.TIME_ZONE = "Europe/Berlin"
        outbox_row()
        served = parse(fetch(api_client).content.decode())[0]
        assert served["Conversion Time"].endswith(("+00:00", "+01:00", "+02:00"))
        assert "T" not in served["Conversion Time"]


def re_time(value: str) -> bool:
    import re

    return bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", value)
    )


# ── The windows ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestClickWindow:
    def test_a_click_past_ninety_days_is_not_in_the_file(self, api_client):
        now = timezone.now()
        outbox_row(clicked_at=now - timedelta(days=91), conversion_at=now - timedelta(days=1))
        assert parse(fetch(api_client).content.decode()) == []

    def test_a_click_inside_ninety_days_is(self, api_client):
        now = timezone.now()
        outbox_row(clicked_at=now - timedelta(days=89), conversion_at=now - timedelta(days=1))
        assert len(parse(fetch(api_client).content.decode())) == 1

    def test_serving_the_feed_settles_the_expired_rows(self, api_client):
        """A pull-only deployment runs no drain. If the feed did not settle
        them, the outbox would only ever grow."""
        now = timezone.now()
        row = outbox_row(
            clicked_at=now - timedelta(days=100), conversion_at=now - timedelta(days=95)
        )
        fetch(api_client)
        row.refresh_from_db()
        assert (row.status, row.reason, row.next_attempt_at) == (
            "skipped", "expired", None,
        )

    def test_an_already_uploaded_row_past_the_window_is_not_re_offered(
        self, api_client
    ):
        now = timezone.now()
        outbox_row(
            status=ConversionUpload.STATUS_UPLOADED,
            clicked_at=now - timedelta(days=100),
            conversion_at=now - timedelta(days=95),
        )
        assert parse(fetch(api_client).content.decode()) == []


@pytest.mark.django_db
class TestFeedWindow:
    def test_the_feed_reaches_further_back_than_the_click_window(
        self, api_client
    ):
        """120 days by default. The puller owns its own schedule, and a feed
        that dropped a row on our retention would make one missed fetch a
        permanently lost conversion."""
        assert feed.window_days() == 120

    def test_a_conversion_older_than_the_feed_window_is_dropped(
        self, api_client, settings
    ):
        settings.STAPEL_ANALYTICS = {
            "CONVERSION_FEED_TOKEN": TOKEN,
            "CONVERSION_FEED_CONVERSION_NAME": NAME,
            "CONVERSION_FEED_WINDOW_DAYS": 5,
        }
        now = timezone.now()
        outbox_row(conversion_at=now - timedelta(days=6), clicked_at=now - timedelta(days=6))
        assert parse(fetch(api_client).content.decode()) == []

    def test_a_re_read_answers_the_same_rows(self, api_client):
        """Serving does not consume. The platform deduplicates; a feed that
        drained on read would lose a conversion to one failed fetch."""
        outbox_row()
        first = fetch(api_client).content.decode()
        second = fetch(api_client).content.decode()
        assert first == second
        assert ConversionUpload.objects.get().status == "pending"


@pytest.mark.django_db
class TestServedStatuses:
    @pytest.mark.parametrize("status", ["pending", "uploaded"])
    def test_pending_and_uploaded_rows_are_served(self, api_client, status):
        outbox_row(status=status)
        assert len(parse(fetch(api_client).content.decode())) == 1

    @pytest.mark.parametrize("status", ["rejected", "skipped"])
    def test_a_settled_refusal_is_not_re_offered(self, api_client, status):
        outbox_row(status=status, reason="whatever")
        assert parse(fetch(api_client).content.decode()) == []


# ── The credential ───────────────────────────────────────────────────


def basic(user: str, password: str) -> str:
    import base64

    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


CHALLENGE = 'Basic realm="conversions feed"'


@pytest.mark.django_db
class TestToken:
    def test_no_token_configured_means_the_endpoint_does_not_exist(
        self, api_client, settings
    ):
        """404, not 401: the feed ships off, and a 401 would advertise that
        there is something here worth getting a token for."""
        settings.STAPEL_ANALYTICS = {}
        assert fetch(api_client, token=None).status_code == 404
        assert fetch(api_client, token=TOKEN).status_code == 404

    def test_a_missing_credential_is_challenged(self, api_client):
        """401 with the Basic challenge: a fetcher that waits to be asked
        before it sends its password needs to see it."""
        response = fetch(api_client, token=None)
        assert response.status_code == 401
        assert response["WWW-Authenticate"] == CHALLENGE

    def test_a_wrong_token_is_challenged(self, api_client):
        response = fetch(api_client, token="not-the-token")
        assert response.status_code == 401
        assert response["WWW-Authenticate"] == CHALLENGE

    def test_an_empty_bearer_is_challenged(self, api_client):
        assert fetch(api_client, token="").status_code == 401

    def test_a_bearer_token_still_answers_the_file(self, api_client):
        assert fetch(api_client).status_code == 200

    def test_a_query_token_works_for_a_puller_that_cannot_send_headers(
        self, api_client
    ):
        assert api_client.get(f"{FEED}?token={TOKEN}").status_code == 200
        assert api_client.get(f"{FEED}?token=wrong").status_code == 401

    def test_the_comparison_is_constant_time(self):
        """A compare that returns early hands the token over one character
        at a time to anybody willing to time it."""
        import inspect

        assert "compare_digest" in inspect.getsource(feed.token_matches)
        assert "compare_digest" in inspect.getsource(feed.credential_matches)

    def test_a_disabled_feed_never_matches_an_empty_token(self, settings):
        settings.STAPEL_ANALYTICS = {}
        assert feed.token_matches("") is False
        assert feed.token_matches(None) is False

    def test_a_refusal_carries_the_module_s_error_key(self, api_client):
        body = fetch(api_client, token="wrong").json()
        assert body["localizable_error"] == "error.401.analytics_feed_token"


@pytest.mark.django_db
class TestBasicAuth:
    """Google's data manager offers a URL, a username and a password —
    no bearer, no custom header — so Basic with the token as the password
    is the door it can walk through."""

    def test_basic_with_the_token_as_password_answers_the_file(self, api_client):
        response = api_client.get(FEED, HTTP_AUTHORIZATION=basic("google", TOKEN))
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")

    def test_any_username_works_by_default(self, api_client):
        for user in ("google", "", "ads-connector", "user with spaces"):
            assert api_client.get(FEED, HTTP_AUTHORIZATION=basic(user, TOKEN)).status_code == 200, user

    def test_a_wrong_password_is_challenged(self, api_client):
        response = api_client.get(FEED, HTTP_AUTHORIZATION=basic("google", "wrong"))
        assert response.status_code == 401
        assert response["WWW-Authenticate"] == CHALLENGE
        assert response.json()["localizable_error"] == "error.401.analytics_feed_token"

    def test_a_malformed_basic_header_is_challenged(self, api_client):
        for value in ("Basic", "Basic not-base64!", "Basic bm9jb2xvbg=="):
            response = api_client.get(FEED, HTTP_AUTHORIZATION=value)
            assert response.status_code == 401, value

    def test_a_pinned_username_is_enforced(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {
            "CONVERSION_FEED_TOKEN": TOKEN,
            "CONVERSION_FEED_CONVERSION_NAME": NAME,
            "CONVERSION_FEED_USERNAME": "google",
        }
        assert api_client.get(FEED, HTTP_AUTHORIZATION=basic("google", TOKEN)).status_code == 200
        assert api_client.get(FEED, HTTP_AUTHORIZATION=basic("other", TOKEN)).status_code == 401

    def test_a_pinned_username_does_not_close_the_bearer_door(self, api_client, settings):
        """A bearer has no username; pinning one is about the Basic pair."""
        settings.STAPEL_ANALYTICS = {
            "CONVERSION_FEED_TOKEN": TOKEN,
            "CONVERSION_FEED_CONVERSION_NAME": NAME,
            "CONVERSION_FEED_USERNAME": "google",
        }
        assert fetch(api_client).status_code == 200

    def test_the_scheme_is_named(self, rf):
        request = rf.get(FEED, HTTP_AUTHORIZATION=basic("google", TOKEN))
        credential = feed.presented_credential(request)
        assert credential.scheme == feed.SCHEME_BASIC
        assert credential.username == "google"
        assert credential.token == TOKEN
        assert feed.presented_credential(rf.get(FEED, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")).scheme == feed.SCHEME_BEARER
        assert feed.presented_credential(rf.get(f"{FEED}?token={TOKEN}")).scheme == feed.SCHEME_QUERY
        assert feed.presented_credential(rf.get(FEED)).scheme == ""


# ── The log line ─────────────────────────────────────────────────────


@pytest.mark.django_db
class TestFetchLog:
    """A pull leaves no trace on the server unless the server writes one.
    The receipt table records served files; the log records every attempt,
    which is where "Google is hitting the URL and getting 401" shows up."""

    def test_a_served_fetch_logs_scheme_agent_and_status(self, api_client, caplog):
        import logging

        outbox_row()
        with caplog.at_level(logging.INFO, logger="stapel_analytics.feed"):
            api_client.get(
                FEED,
                HTTP_AUTHORIZATION=basic("google", TOKEN),
                HTTP_USER_AGENT="Google-Ads-Data-Manager/1.0",
            )
        line = [r for r in caplog.records if r.name == "stapel_analytics.feed"][-1].getMessage()
        assert "scheme=basic" in line
        assert "status=200" in line
        assert "rows=1" in line
        assert "Google-Ads-Data-Manager/1.0" in line

    def test_a_refusal_is_logged_too(self, api_client, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="stapel_analytics.feed"):
            api_client.get(FEED, HTTP_AUTHORIZATION=basic("google", "wrong"))
            api_client.get(FEED)
        lines = [r.getMessage() for r in caplog.records if r.name == "stapel_analytics.feed"]
        assert any("scheme=basic status=401" in line for line in lines)
        assert any("scheme=none status=401" in line for line in lines)

    def test_the_credential_is_never_logged(self, api_client, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="stapel_analytics.feed"):
            api_client.get(FEED, HTTP_AUTHORIZATION=basic("google", TOKEN))
            api_client.get(FEED, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
            api_client.get(f"{FEED}?token={TOKEN}")
            api_client.get(FEED, HTTP_AUTHORIZATION=basic("google", "wrong-secret"))
        assert TOKEN not in caplog.text
        assert "wrong-secret" not in caplog.text


# ── The response headers ─────────────────────────────────────────────


@pytest.mark.django_db
class TestResponseHeaders:
    def test_the_file_is_never_cached(self, api_client):
        """A cached copy is a file that reports yesterday's conversions
        forever, to a fetcher whose caching rules we cannot see."""
        assert fetch(api_client)["Cache-Control"] == "no-store"

    def test_it_is_offered_as_a_named_file(self, api_client):
        assert "google-ads-conversions.csv" in fetch(api_client)["Content-Disposition"]


# ── The receipt ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestFetchRecord:
    def test_a_served_fetch_is_written_down(self, api_client):
        outbox_row()
        fetch(api_client)
        record = ConversionFeedFetch.objects.get()
        assert record.rows == 1
        assert record.at is not None

    def test_an_empty_file_is_still_a_fetch(self, api_client):
        """Zero rows served is the answer to "is the puller working", and a
        receipt that only appeared when there was data could not give it."""
        fetch(api_client)
        assert ConversionFeedFetch.objects.get().rows == 0

    def test_a_refused_read_leaves_no_receipt(self, api_client):
        fetch(api_client, token="wrong")
        fetch(api_client, token=None)
        assert ConversionFeedFetch.objects.count() == 0

    def test_the_remote_address_is_recorded(self, api_client):
        fetch(api_client, HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")
        assert ConversionFeedFetch.objects.get().remote == "203.0.113.7"

    def test_receipts_older_than_the_history_are_trimmed(self, api_client):
        old = ConversionFeedFetch.objects.create(rows=1)
        ConversionFeedFetch.objects.filter(pk=old.pk).update(
            at=timezone.now() - timedelta(days=feed.FETCH_HISTORY_DAYS + 1)
        )
        fetch(api_client)
        assert list(ConversionFeedFetch.objects.values_list("pk", flat=True)) != [old.pk]
        assert ConversionFeedFetch.objects.filter(pk=old.pk).exists() is False


@pytest.mark.django_db
class TestFeedStatus:
    def test_it_answers_never_before_the_first_fetch(self):
        assert feed.feed_status()["last_fetch_at"] is None

    def test_it_answers_the_last_fetch_after_one(self, api_client):
        outbox_row()
        fetch(api_client)
        status = feed.feed_status()
        assert status["last_fetch_rows"] == 1
        assert status["last_fetch_at"] is not None
        assert status["enabled"] is True
        assert status["conversion_name"] == NAME

    def test_the_command_prints_the_last_fetch(self, api_client):
        outbox_row()
        fetch(api_client)
        out = _StringIO()
        call_command("analytics_conversion_feed_status", stdout=out)
        printed = out.getvalue()
        assert "rows served       1" in printed
        assert NAME in printed

    def test_the_command_says_never_when_nothing_has_read_it(self):
        out = _StringIO()
        call_command("analytics_conversion_feed_status", stdout=out)
        assert "NEVER" in out.getvalue()

    def test_the_command_never_prints_the_token(self, settings):
        out = _StringIO()
        call_command("analytics_conversion_feed_status", stdout=out)
        assert TOKEN not in out.getvalue()

    def test_a_disabled_feed_says_so(self, settings):
        settings.STAPEL_ANALYTICS = {}
        out = _StringIO()
        call_command("analytics_conversion_feed_status", stdout=out)
        assert "DISABLED" in out.getvalue()


# ── Expiry, on both doors ────────────────────────────────────────────


@pytest.mark.django_db
class TestExpiry:
    def test_a_click_past_the_window_expires_rather_than_retrying(self):
        now = timezone.now()
        row = outbox_row(
            clicked_at=now - timedelta(days=100), conversion_at=now - timedelta(days=95)
        )
        assert conversions.expire_stale() == 1
        row.refresh_from_db()
        assert (row.status, row.reason) == ("skipped", "expired")

    def test_expiry_is_terminal(self):
        """Nothing later makes a click younger, so the row is settled and
        `due()` never returns it again."""
        now = timezone.now()
        outbox_row(clicked_at=now - timedelta(days=100), conversion_at=now - timedelta(days=95))
        conversions.expire_stale()
        assert conversions.due() == []
        assert conversions.expire_stale() == 0

    def test_a_live_row_is_left_alone(self):
        outbox_row()
        assert conversions.expire_stale() == 0
        assert ConversionUpload.objects.get().status == "pending"

    def test_the_upload_task_expires_before_it_uploads(self):
        """The drain is the other place rows go stale, and a row that can
        never go must not spend the limit of a pass that could have sent a
        row that can."""
        now = timezone.now()
        outbox_row(clicked_at=now - timedelta(days=100), conversion_at=now - timedelta(days=95))
        assert tasks.upload_click_conversions() == {"expired": 1}

    def test_expired_is_not_the_same_answer_as_window(self):
        """`window` is the caller's data (the conversion came too long after
        its click). `expired` is our latency (nobody reported it in time).
        A backlog that could not tell them apart could not be acted on."""
        now = timezone.now()
        assert conversions.window_verdict(now, now - timedelta(days=91)) == "window"
        assert conversions.expiry_verdict(now, now - timedelta(days=91)) == "expired"
        assert (
            conversions.expiry_verdict(
                now - timedelta(days=95), now - timedelta(days=100)
            )
            == "expired"
        )
        assert (
            conversions.window_verdict(
                now - timedelta(days=95), now - timedelta(days=100)
            )
            is None
        )

    def test_a_settled_row_leaves_the_feed(self, api_client):
        now = timezone.now()
        outbox_row(clicked_at=now - timedelta(days=100), conversion_at=now - timedelta(days=95))
        assert parse(fetch(api_client).content.decode()) == []


# ── Configuration ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestConfigurationCheck:
    def test_the_placeholder_name_is_warned_about(self, settings):
        from django.core.checks import run_checks

        settings.STAPEL_ANALYTICS = {"CONVERSION_FEED_TOKEN": TOKEN}
        ids = [warning.id for warning in run_checks()]
        assert "analytics.W012" in ids

    def test_a_named_feed_is_not_warned_about(self, settings):
        from django.core.checks import run_checks

        ids = [warning.id for warning in run_checks()]
        assert "analytics.W012" not in ids

    def test_a_disabled_feed_is_not_warned_about(self, settings):
        from django.core.checks import run_checks

        settings.STAPEL_ANALYTICS = {}
        ids = [warning.id for warning in run_checks()]
        assert "analytics.W012" not in ids


class TestFeedMount:
    def test_the_feed_can_be_mounted_alone(self):
        """A host that installs this module only to hand a platform a file
        must not get an anonymous ingest route as a side effect."""
        from stapel_analytics import urls_feed, urls_v1

        assert urls_feed.FEED_PATTERNS is urls_v1.FEED_PATTERNS
        assert len(urls_feed.urlpatterns) == 1

    def test_the_feed_is_its_own_capability_gate(self):
        from stapel_analytics.urls_v1 import GATE_REGISTRY

        gate = GATE_REGISTRY["analytics.conversion_feed"]
        assert gate.patterns
        assert all(
            entry not in GATE_REGISTRY["analytics.api"].patterns
            for entry in gate.patterns
        )
