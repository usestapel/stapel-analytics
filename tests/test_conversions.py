"""Offline click-conversion upload to Google Ads.

The client is stubbed at exactly one seam — ``conversions._client_class``,
the single place the optional SDK is named — so nothing here reaches the
network and the suite runs without ``google-ads`` installed. What the stub
records is the REQUEST, because the request is the contract: which field a
click id landed in, what the timestamp looked like, what the value and
currency were. A test that only asserted "upload_click_conversions was
called" would pass with every one of those wrong.
"""
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from stapel_core.comm import call

from stapel_analytics import conversions
from stapel_analytics.models import ConversionUpload

CREDENTIALS = {
    "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token",
    "GOOGLE_ADS_CLIENT_ID": "client-id",
    "GOOGLE_ADS_CLIENT_SECRET": "client-secret",
    "GOOGLE_ADS_REFRESH_TOKEN": "refresh-token",
    "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
}

ACTION = "customers/1234567890/conversionActions/42"


class Message:
    """A permissive stand-in for a protobuf message: any field, any value."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakeService:
    def __init__(self, sink):
        self.sink = sink

    def upload_click_conversions(self, request=None):
        self.sink["requests"].append(request)
        outcome = self.sink.get("outcome")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        return Message(partial_failure_error=None, results=[Message()])


class FakeClient:
    def __init__(self, sink):
        self.sink = sink

    def get_type(self, name):
        if name == "UploadClickConversionsRequest":
            return Message(customer_id="", conversions=[], partial_failure=False)
        return Message()

    def get_service(self, name):
        self.sink["service"] = name
        return FakeService(self.sink)


@pytest.fixture
def ads(monkeypatch, settings):
    """A configured deployment whose Google Ads client records instead of calling."""
    settings.STAPEL_ANALYTICS = dict(CREDENTIALS)
    sink = {"requests": []}

    class Loader:
        @staticmethod
        def load_from_dict(config, version=None):
            sink["config"] = config
            sink["version"] = version
            return FakeClient(sink)

    monkeypatch.setattr(conversions, "_client_class", lambda: Loader)
    return sink


def enqueue_and_deliver(**overrides):
    payload = {
        "click_id": "CJ-click-1",
        "click_id_type": "gclid",
        "conversion_action": ACTION,
        "conversion_at": timezone.now().isoformat(),
    }
    payload.update(overrides)
    return call("analytics.upload_click_conversion", payload)


# ── The request Google actually receives ─────────────────────────────


@pytest.mark.django_db
class TestClickIdMapping:
    @pytest.mark.parametrize("kind", ["gclid", "gbraid", "wbraid"])
    def test_each_kind_lands_in_its_own_field(self, ads, kind):
        """Google models the three as separate fields, not as a value plus a
        discriminator: a gbraid written to `gclid` is a rejected upload."""
        assert enqueue_and_deliver(click_id_type=kind, click_id="id-1")[
            "status"
        ] == "uploaded"
        conversion = ads["requests"][0].conversions[0]
        assert getattr(conversion, kind) == "id-1"

    @pytest.mark.parametrize("kind", ["gclid", "gbraid", "wbraid"])
    def test_the_other_two_fields_are_never_set(self, ads, kind):
        enqueue_and_deliver(click_id_type=kind, click_id="id-1")
        conversion = ads["requests"][0].conversions[0]
        for other in set(conversions.CLICK_ID_FIELDS) - {kind}:
            assert not hasattr(conversion, other), other

    def test_the_mapping_covers_every_model_choice(self):
        """A choice with no request field is a row that can only ever fail."""
        assert set(conversions.CLICK_ID_FIELDS) == {
            value for value, _label in ConversionUpload.CLICK_ID_TYPES
        }

    def test_an_unknown_kind_is_refused_at_enqueue(self):
        with pytest.raises(ValueError):
            conversions.enqueue(
                click_id="x", click_id_type="fclid", conversion_at=timezone.now()
            )


@pytest.mark.django_db
class TestRequestShape:
    def test_the_conversion_action_travels(self, ads):
        enqueue_and_deliver()
        assert ads["requests"][0].conversions[0].conversion_action == ACTION

    def test_the_customer_id_is_digits_only(self, ads):
        """Google prints it 123-456-7890 and refuses it that way."""
        enqueue_and_deliver()
        assert ads["requests"][0].customer_id == "1234567890"

    def test_the_service_is_the_conversion_upload_service(self, ads):
        enqueue_and_deliver()
        assert ads["service"] == "ConversionUploadService"

    def test_partial_failure_is_on(self, ads):
        """It is what turns a validation refusal into data instead of an
        SDK exception class this module would have to import to catch."""
        enqueue_and_deliver()
        assert ads["requests"][0].partial_failure is True

    def test_the_value_and_currency_travel(self, ads):
        enqueue_and_deliver(value=19.99, currency="EUR")
        conversion = ads["requests"][0].conversions[0]
        assert conversion.conversion_value == pytest.approx(19.99)
        assert conversion.currency_code == "EUR"

    def test_a_valueless_conversion_carries_no_value(self, ads):
        """A lead is a conversion that counts and is worth no money; sending
        0.0 would be a number nobody measured."""
        enqueue_and_deliver()
        assert not hasattr(ads["requests"][0].conversions[0], "conversion_value")

    def test_the_api_version_is_pinned_when_configured(self, ads, settings):
        settings.STAPEL_ANALYTICS = {**CREDENTIALS, "GOOGLE_ADS_API_VERSION": "v18"}
        enqueue_and_deliver()
        assert ads["version"] == "v18"

    def test_the_login_customer_id_is_sent_only_when_set(self, ads, settings):
        enqueue_and_deliver()
        assert "login_customer_id" not in ads["config"]
        settings.STAPEL_ANALYTICS = {
            **CREDENTIALS, "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "111-222-3333"
        }
        enqueue_and_deliver(click_id="CJ-click-2")
        assert ads["config"]["login_customer_id"] == "1112223333"


class TestGoogleDatetime:
    PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_the_format_is_the_documented_one(self):
        assert self.PATTERN.match(conversions.google_datetime(timezone.now()))

    def test_a_naive_stamp_gains_the_deployment_offset(self):
        from datetime import datetime

        assert self.PATTERN.match(
            conversions.google_datetime(datetime(2026, 3, 4, 5, 6, 7))
        )

    @pytest.mark.django_db
    def test_the_uploaded_stamp_matches_the_row(self, ads):
        moment = timezone.now().replace(microsecond=0)
        enqueue_and_deliver(conversion_at=moment.isoformat())
        sent = ads["requests"][0].conversions[0].conversion_date_time
        assert sent == conversions.google_datetime(moment)


# ── The 90-day window ────────────────────────────────────────────────


@pytest.mark.django_db
class TestWindow:
    def test_a_click_older_than_the_window_is_skipped(self, ads):
        now = timezone.now()
        answer = enqueue_and_deliver(
            conversion_at=now.isoformat(),
            clicked_at=(now - timedelta(days=91)).isoformat(),
        )
        assert answer == {"status": "skipped", "reason": "window"}
        assert ads["requests"] == []

    def test_a_click_inside_the_window_is_uploaded(self, ads):
        now = timezone.now()
        answer = enqueue_and_deliver(
            conversion_at=now.isoformat(),
            clicked_at=(now - timedelta(days=89)).isoformat(),
        )
        assert answer["status"] == "uploaded"

    def test_without_a_click_time_the_conversion_age_is_the_fallback(self, ads):
        answer = enqueue_and_deliver(
            conversion_at=(timezone.now() - timedelta(days=91)).isoformat()
        )
        assert answer == {"status": "skipped", "reason": "window"}

    def test_the_fallback_is_weaker_and_the_module_says_so(self, ads):
        """A recent conversion on a year-old click passes the fallback and is
        sent — Google rejects it with its own reason rather than this module
        pretending it enforced a rule it cannot measure."""
        answer = enqueue_and_deliver(conversion_at=timezone.now().isoformat())
        assert answer["status"] == "uploaded"
        assert "weaker" in conversions.__doc__

    def test_the_window_is_a_setting(self, ads, settings):
        settings.STAPEL_ANALYTICS = {
            **CREDENTIALS, "GOOGLE_ADS_CONVERSION_WINDOW_DAYS": 7
        }
        answer = enqueue_and_deliver(
            conversion_at=(timezone.now() - timedelta(days=8)).isoformat()
        )
        assert answer["reason"] == "window"

    def test_a_skipped_row_is_terminal(self, ads):
        enqueue_and_deliver(
            conversion_at=(timezone.now() - timedelta(days=91)).isoformat()
        )
        row = ConversionUpload.objects.get()
        assert (row.status, row.next_attempt_at) == ("skipped", None)


# ── Not configured ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestNotConfigured:
    def test_missing_credentials_skip(self, settings):
        settings.STAPEL_ANALYTICS = {}
        assert enqueue_and_deliver() == {
            "status": "skipped", "reason": "not_configured"
        }

    def test_a_credential_backlog_stays_pending(self, settings):
        """Credentials arriving tomorrow must still upload today's
        conversions — they are well inside the 90-day window."""
        settings.STAPEL_ANALYTICS = {}
        enqueue_and_deliver()
        row = ConversionUpload.objects.get()
        assert row.status == "pending"
        assert row.reason == "not_configured"
        assert row.next_attempt_at is not None

    def test_the_backlog_uploads_once_configured(self, ads, settings):
        settings.STAPEL_ANALYTICS = {}
        enqueue_and_deliver()
        settings.STAPEL_ANALYTICS = dict(CREDENTIALS)
        row = ConversionUpload.objects.get()
        row.next_attempt_at = None
        row.save(update_fields=["next_attempt_at"])
        assert conversions.deliver(row)["status"] == "uploaded"

    def test_a_partial_credential_set_is_not_configured(self, settings):
        settings.STAPEL_ANALYTICS = {
            key: value for key, value in CREDENTIALS.items()
            if key != "GOOGLE_ADS_REFRESH_TOKEN"
        }
        assert conversions.is_configured() is False

    def test_a_blank_conversion_action_is_terminal(self, ads):
        """No configuration change supplies an action this row never had."""
        answer = enqueue_and_deliver(conversion_action="")
        assert answer == {"status": "skipped", "reason": "not_configured"}
        assert ConversionUpload.objects.get().status == "skipped"


# ── Idempotence ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestIdempotence:
    def test_the_same_conversion_enqueues_one_row(self, ads):
        moment = timezone.now().isoformat()
        enqueue_and_deliver(conversion_at=moment)
        enqueue_and_deliver(conversion_at=moment)
        assert ConversionUpload.objects.count() == 1

    def test_a_repeat_does_not_upload_twice(self, ads):
        moment = timezone.now().isoformat()
        enqueue_and_deliver(conversion_at=moment)
        assert enqueue_and_deliver(conversion_at=moment)["status"] == "uploaded"
        assert len(ads["requests"]) == 1

    def test_delivering_a_settled_row_sends_nothing(self, ads):
        enqueue_and_deliver()
        row = ConversionUpload.objects.get()
        assert conversions.deliver(row)["status"] == "uploaded"
        assert len(ads["requests"]) == 1

    def test_a_different_conversion_time_is_a_different_conversion(self, ads):
        now = timezone.now()
        enqueue_and_deliver(conversion_at=now.isoformat())
        enqueue_and_deliver(conversion_at=(now - timedelta(hours=1)).isoformat())
        assert ConversionUpload.objects.count() == 2

    def test_a_repeat_does_not_overwrite_the_stored_value(self, ads):
        moment = timezone.now().isoformat()
        enqueue_and_deliver(conversion_at=moment, value=10)
        enqueue_and_deliver(conversion_at=moment, value=999)
        assert ConversionUpload.objects.get().value == Decimal("10.0000")


# ── Rejection, retry, backoff ────────────────────────────────────────


@pytest.mark.django_db
class TestRejection:
    def test_googles_reason_travels_verbatim(self, ads):
        ads["outcome"] = Message(
            partial_failure_error=Message(message="CLICK_NOT_FOUND"), results=[]
        )
        assert enqueue_and_deliver() == {
            "status": "rejected", "reason": "CLICK_NOT_FOUND"
        }

    def test_a_rejection_is_terminal(self, ads):
        ads["outcome"] = Message(
            partial_failure_error=Message(message="EXPIRED_CLICK"), results=[]
        )
        enqueue_and_deliver()
        row = ConversionUpload.objects.get()
        assert (row.status, row.next_attempt_at) == ("rejected", None)

    def test_a_response_with_neither_result_nor_error_is_not_success(self, ads):
        ads["outcome"] = Message(partial_failure_error=None, results=[])
        assert enqueue_and_deliver()["reason"] == "no_result"


@pytest.mark.django_db
class TestRetry:
    def test_a_transport_failure_stays_pending(self, ads):
        ads["outcome"] = RuntimeError("connection reset")
        answer = enqueue_and_deliver()
        assert answer["status"] == "pending"
        assert "connection reset" in answer["reason"]

    def test_it_is_not_reported_as_a_rejection(self, ads):
        """"Google said no" and "we could not ask" must not share a status."""
        ads["outcome"] = RuntimeError("connection reset")
        assert enqueue_and_deliver()["status"] != "rejected"

    def test_the_attempt_is_counted_and_backed_off(self, ads):
        ads["outcome"] = RuntimeError("boom")
        enqueue_and_deliver()
        row = ConversionUpload.objects.get()
        assert row.attempts == 1
        assert row.next_attempt_at > timezone.now()

    def test_the_backoff_grows(self):
        from stapel_analytics.conf import analytics_settings

        base = analytics_settings.GOOGLE_ADS_RETRY_BASE_SECONDS
        assert conversions._backoff(1) == timedelta(seconds=base)
        assert conversions._backoff(3) == timedelta(seconds=base * 4)

    def test_the_backoff_is_capped(self, settings):
        settings.STAPEL_ANALYTICS = {**CREDENTIALS, "GOOGLE_ADS_RETRY_MAX_SECONDS": 60}
        assert conversions._backoff(20) == timedelta(seconds=60)

    def test_a_row_in_backoff_is_not_due(self, ads):
        ads["outcome"] = RuntimeError("boom")
        enqueue_and_deliver()
        assert conversions.due(10) == []

    def test_a_row_past_its_backoff_is_due(self, ads):
        ads["outcome"] = RuntimeError("boom")
        enqueue_and_deliver()
        ConversionUpload.objects.update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        assert len(conversions.due(10)) == 1

    def test_the_retry_uploads_and_does_not_double_upload(self, ads):
        ads["outcome"] = RuntimeError("boom")
        enqueue_and_deliver()
        ads["outcome"] = None
        ConversionUpload.objects.update(next_attempt_at=None)
        row = ConversionUpload.objects.get()
        assert conversions.deliver(row)["status"] == "uploaded"
        assert conversions.deliver(row)["status"] == "uploaded"
        assert len(ads["requests"]) == 2  # the failed attempt, then the good one

    def test_the_attempt_budget_ends_the_row(self, ads, settings):
        settings.STAPEL_ANALYTICS = {**CREDENTIALS, "GOOGLE_ADS_MAX_ATTEMPTS": 2}
        ads["outcome"] = RuntimeError("boom")
        enqueue_and_deliver()
        row = ConversionUpload.objects.get()
        row.next_attempt_at = None
        row.save(update_fields=["next_attempt_at"])
        answer = conversions.deliver(row)
        assert answer["status"] == "rejected"
        assert answer["reason"].startswith("max_attempts")

    def test_the_sdk_absence_is_retryable_not_a_verdict(self, monkeypatch, settings):
        settings.STAPEL_ANALYTICS = dict(CREDENTIALS)

        def missing():
            raise conversions.GoogleAdsUnavailable("no SDK")

        monkeypatch.setattr(conversions, "_client_class", missing)
        assert enqueue_and_deliver()["status"] == "pending"


# ── The command ──────────────────────────────────────────────────────


def run(command, *args, **options):
    out = StringIO()
    call_command(command, *args, stdout=out, **options)
    return out.getvalue()


@pytest.mark.django_db
class TestUploadCommand:
    def test_an_empty_outbox_says_so(self, ads):
        assert "no conversion uploads are due" in run("analytics_upload_conversions")

    def test_it_uploads_the_due_rows(self, ads, settings):
        settings.STAPEL_ANALYTICS = {}
        enqueue_and_deliver()
        settings.STAPEL_ANALYTICS = dict(CREDENTIALS)
        ConversionUpload.objects.update(next_attempt_at=None)
        output = run("analytics_upload_conversions")
        assert "1 conversion upload(s) attempted" in output
        assert "uploaded   1" in output
        assert ConversionUpload.objects.get().status == "uploaded"

    def test_the_limit_bounds_the_pass(self, ads, settings):
        settings.STAPEL_ANALYTICS = {}
        now = timezone.now()
        for index in range(3):
            enqueue_and_deliver(
                click_id=f"c-{index}",
                conversion_at=(now - timedelta(minutes=index)).isoformat(),
            )
        settings.STAPEL_ANALYTICS = dict(CREDENTIALS)
        ConversionUpload.objects.update(next_attempt_at=None)
        assert "1 conversion upload(s) attempted" in run(
            "analytics_upload_conversions", "--limit", "1"
        )
        assert ConversionUpload.objects.filter(status="uploaded").count() == 1


@pytest.mark.django_db
class TestDryRun:
    @pytest.fixture
    def pending(self, ads, settings):
        settings.STAPEL_ANALYTICS = {}
        enqueue_and_deliver()
        settings.STAPEL_ANALYTICS = dict(CREDENTIALS)
        ConversionUpload.objects.update(next_attempt_at=None)
        return ConversionUpload.objects.get()

    def test_it_lists_the_row(self, pending):
        assert "CJ-click-1" in run("analytics_upload_conversions", "--dry-run")

    def test_it_says_it_wrote_nothing(self, pending):
        assert "dry run — nothing was written" in run(
            "analytics_upload_conversions", "--dry-run"
        )

    def test_it_uploads_nothing(self, ads, pending):
        run("analytics_upload_conversions", "--dry-run")
        assert ads["requests"] == []

    def test_it_writes_nothing_at_all(self, pending):
        """No status, no reason, no attempt counter, no next-attempt stamp —
        a dry run that bumped the counter would spend an attempt from the
        budget that decides when a row is given up on."""
        before = ConversionUpload.objects.values().get()
        run("analytics_upload_conversions", "--dry-run")
        assert ConversionUpload.objects.values().get() == before

    def test_it_names_an_incomplete_configuration(self, pending, settings):
        settings.STAPEL_ANALYTICS = {}
        assert "INCOMPLETE" in run("analytics_upload_conversions", "--dry-run")


# ── The comm contract ────────────────────────────────────────────────


@pytest.mark.django_db
class TestCommSurface:
    def test_the_function_has_a_committed_schema(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        assert (
            root / "schemas" / "functions" / "analytics.upload_click_conversion.json"
        ).is_file()

    def test_a_minimal_payload_validates(self, ads):
        assert call(
            "analytics.upload_click_conversion",
            {"click_id": "c", "conversion_action": ACTION,
             "conversion_at": timezone.now().isoformat()},
        )["status"] == "uploaded"

    def test_the_default_click_id_type_is_gclid(self, ads):
        call(
            "analytics.upload_click_conversion",
            {"click_id": "CJ-click-1", "conversion_action": ACTION,
             "conversion_at": timezone.now().isoformat()},
        )
        assert ads["requests"][0].conversions[0].gclid == "CJ-click-1"

    def test_an_undeclared_field_is_refused(self, ads):
        with pytest.raises(Exception):
            call(
                "analytics.upload_click_conversion",
                {"click_id": "c", "conversion_at": timezone.now().isoformat(),
                 "nonsense": 1},
            )

    def test_an_unknown_click_id_type_is_refused_by_the_schema(self, ads):
        with pytest.raises(Exception):
            call(
                "analytics.upload_click_conversion",
                {"click_id": "c", "click_id_type": "fclid",
                 "conversion_at": timezone.now().isoformat()},
            )

    def test_a_missing_click_id_is_refused_by_the_schema(self, ads):
        with pytest.raises(Exception):
            call(
                "analytics.upload_click_conversion",
                {"conversion_at": timezone.now().isoformat()},
            )


# ── The boot check ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestCredentialCheck:
    def test_a_backlog_without_credentials_warns(self, settings):
        from stapel_analytics.checks import check_conversion_upload_credentials

        settings.STAPEL_ANALYTICS = {}
        enqueue_and_deliver()
        assert [w.id for w in check_conversion_upload_credentials(None)] == [
            "analytics.W011"
        ]

    def test_no_backlog_no_warning(self, settings):
        from stapel_analytics.checks import check_conversion_upload_credentials

        settings.STAPEL_ANALYTICS = {}
        assert check_conversion_upload_credentials(None) == []

    def test_a_configured_deployment_is_silent(self, ads):
        from stapel_analytics.checks import check_conversion_upload_credentials

        enqueue_and_deliver()
        assert check_conversion_upload_credentials(None) == []
