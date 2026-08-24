"""Wire compatibility with the shipped ``@stapel/analytics`` facade.

The frontend half of the analytics standard shipped FIRST. These tests are
the contract in the direction that matters: payloads here are transcribed
from ``@stapel/analytics``' own source, not from this module's docs, so a
change on either side that breaks the join fails here.

Sources (stapel-react/packages/analytics):

- ``createAnalytics.ts`` ``enqueue()`` — the event shape
  ``{id, kind, name, props, userHash?, ts}`` with ``ts = Date.now()``
  (epoch MILLISECONDS) and ``id = `${Date.now()}-${seq}```;
- ``providers.ts`` ``stapelCollectorProvider`` — the batch shape
  ``{events: [...], write_key?}`` POSTed to
  ``COLLECTOR_PATH = "/analytics/api/events"``;
- ``hash.ts`` ``sha256Hex`` — an UNSALTED sha256 hex of the user id;
- ``pii.ts`` — values are guarded, keys are not.
"""
import hashlib
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_analytics.privacy import hash_user_id
from stapel_analytics.store import iter_events

#: The literal path ``providers.ts`` hardcodes.
COLLECTOR_PATH = "/analytics/api/events"
#: The canon path of the fleet (api-versioning §2).
CANON_PATH = "/analytics/api/v1/events"

USER_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
USER_HASH = hashlib.sha256(USER_ID.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {
        "EVENTS": {
            "listing.published": {
                "description": "A seller published a listing",
                "props": {"listing_id": {"type": "string", "description": "id"}},
                "flow": "sell",
            }
        }
    }


def facade_batch():
    """A batch exactly as ``createAnalytics`` + ``stapelCollectorProvider``
    would produce it after ``track`` / ``page`` / ``identify``."""
    now_ms = int(timezone.now().timestamp() * 1000)
    return {
        "events": [
            {
                "id": f"{now_ms}-1",
                "kind": "track",
                "name": "listing.published",
                "props": {"listing_id": "abc-123"},
                "userHash": USER_HASH,
                "ts": now_ms,
            },
            {
                "id": f"{now_ms}-2",
                "kind": "page",
                "name": "/listings/abc-123",
                "props": {"referrer": "/search"},
                "ts": now_ms + 5,
            },
            {
                "id": f"{now_ms}-3",
                "kind": "identify",
                "name": "identify",
                "props": {"plan": "pro"},
                "userHash": USER_HASH,
                "ts": now_ms + 10,
            },
        ]
    }


@pytest.mark.django_db
class TestFacadeBatch:
    def test_the_canon_path_accepts_the_facade_batch(self, api_client):
        response = api_client.post(CANON_PATH, facade_batch(), format="json")
        assert response.status_code == 202
        assert response.json()["accepted"] == 3
        assert response.json()["rejected"] == []

    def test_the_legacy_alias_accepts_the_same_batch(self, api_client):
        """`providers.ts` hardcodes the un-versioned path; it shipped first."""
        response = api_client.post(COLLECTOR_PATH, facade_batch(), format="json")
        assert response.status_code == 202
        assert response.json()["accepted"] == 3

    def test_both_paths_reach_the_same_view(self, api_client):
        api_client.post(CANON_PATH, facade_batch(), format="json")
        api_client.post(COLLECTOR_PATH, facade_batch(), format="json")
        assert len(list(iter_events())) == 6

    def test_camel_case_user_hash_is_stored_snake_case(self, api_client):
        api_client.post(CANON_PATH, facade_batch(), format="json")
        rows = [row for row in iter_events() if row["kind"] == "track"]
        assert rows[0]["user_hash"] == USER_HASH

    def test_epoch_milliseconds_become_the_row_timestamp(self, api_client):
        batch = facade_batch()
        api_client.post(CANON_PATH, batch, format="json")
        expected = batch["events"][0]["ts"] / 1000
        row = next(iter(iter_events()))
        assert abs(row["ts"].timestamp() - expected) < 1

    def test_the_facade_event_id_survives(self, api_client):
        batch = facade_batch()
        api_client.post(CANON_PATH, batch, format="json")
        ids = {row["event_id"] for row in iter_events()}
        assert {e["id"] for e in batch["events"]} == ids

    def test_page_events_keep_their_path_as_a_name(self, api_client):
        api_client.post(CANON_PATH, facade_batch(), format="json")
        pages = [row for row in iter_events() if row["kind"] == "page"]
        assert pages[0]["name"] == "/listings/abc-123"

    def test_page_events_are_not_marked_unregistered(self, api_client):
        api_client.post(CANON_PATH, facade_batch(), format="json")
        pages = [row for row in iter_events() if row["kind"] == "page"]
        assert "unregistered" not in pages[0]

    def test_identify_traits_land_in_props(self, api_client):
        api_client.post(CANON_PATH, facade_batch(), format="json")
        rows = [row for row in iter_events() if row["kind"] == "identify"]
        assert rows[0]["props"] == {"plan": "pro"}

    def test_the_default_source_is_attributed(self, api_client):
        api_client.post(CANON_PATH, facade_batch(), format="json")
        assert {row["source"] for row in iter_events()} == {"web"}


@pytest.mark.django_db
class TestWriteKey:
    def test_the_facade_write_key_names_the_source(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "WRITE_KEYS": {"wk_live_123": "web-spa"},
        }
        batch = {**facade_batch(), "write_key": "wk_live_123"}
        response = api_client.post(CANON_PATH, batch, format="json")
        assert response.json()["source"] == "web-spa"
        assert {row["source"] for row in iter_events()} == {"web-spa"}

    def test_an_absent_write_key_is_accepted_by_default(self, api_client):
        """The facade omits it unless a host configured one."""
        assert api_client.post(
            CANON_PATH, facade_batch(), format="json"
        ).status_code == 202

    def test_a_required_write_key_refuses_the_batch(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "REQUIRE_WRITE_KEY": True,
            "WRITE_KEYS": {"wk": "web"},
        }
        response = api_client.post(CANON_PATH, facade_batch(), format="json")
        assert response.status_code == 401
        assert response.json()["localizable_error"] == "error.401.analytics_write_key"

    def test_a_required_write_key_accepts_a_valid_one(self, api_client, settings):
        settings.STAPEL_ANALYTICS = {
            **settings.STAPEL_ANALYTICS,
            "REQUIRE_WRITE_KEY": True,
            "WRITE_KEYS": {"wk": "web"},
        }
        batch = {**facade_batch(), "write_key": "wk"}
        assert api_client.post(CANON_PATH, batch, format="json").status_code == 202


@pytest.mark.django_db
class TestBeaconShape:
    def test_a_sendbeacon_body_is_accepted(self, api_client):
        """`navigator.sendBeacon(endpoint, new Blob([...], {type: 'application/json'}))`.

        No session, no CSRF token, no auth header — which is exactly why the
        ingest view resolves an EMPTY authenticator list by default.
        """
        response = api_client.post(
            COLLECTOR_PATH,
            data=json.dumps(facade_batch()),
            content_type="application/json",
        )
        assert response.status_code == 202

    def test_an_anonymous_client_is_accepted(self, api_client):
        assert api_client.post(
            CANON_PATH, facade_batch(), format="json"
        ).status_code == 202

    def test_the_response_is_ok_for_response_ok(self, api_client):
        """`providers.ts` only checks `response.ok` — 2xx is the contract."""
        response = api_client.post(CANON_PATH, facade_batch(), format="json")
        assert 200 <= response.status_code < 300


@pytest.mark.django_db
class TestLegacyAliasSwitch:
    def test_the_alias_can_be_switched_off(self, settings):
        """A deployment whose frontend targets v1 drops the alias."""
        import importlib

        from django.urls import clear_url_caches

        from stapel_analytics import urls as urls_module

        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS,
                                     "LEGACY_INGEST_ALIAS": False}
        try:
            reloaded = importlib.reload(urls_module)
            names = {getattr(p, "name", None) for p in reloaded.urlpatterns}
            assert "analytics-ingest-legacy" not in names
        finally:
            settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS,
                                         "LEGACY_INGEST_ALIAS": True}
            importlib.reload(urls_module)
            clear_url_caches()

    def test_the_alias_is_mounted_by_default(self):
        from stapel_analytics import urls as urls_module

        names = {getattr(p, "name", None) for p in urls_module.urlpatterns}
        assert "analytics-ingest-legacy" in names


@pytest.mark.django_db
class TestCrossTierJoin:
    """The whole point of the hash contract: a server step and a browser
    step of the same person land on one funnel subject."""

    def test_a_server_track_shares_the_subject_of_a_browser_event(self, api_client):
        from stapel_analytics import services

        api_client.post(CANON_PATH, facade_batch(), format="json")
        services.track("payment.completed", {"amount": 10}, user_id=USER_ID)

        hashes = {row.get("user_hash") for row in iter_events()}
        assert USER_HASH in hashes
        assert hash_user_id(USER_ID) == USER_HASH

    def test_a_salted_deployment_does_not_join(self, api_client, settings):
        from stapel_analytics import services

        api_client.post(CANON_PATH, facade_batch(), format="json")
        settings.STAPEL_ANALYTICS = {**settings.STAPEL_ANALYTICS,
                                     "USER_HASH_SALT": "pepper"}
        services.track("payment.completed", {}, user_id=USER_ID)
        server_rows = [r for r in iter_events() if r["name"] == "payment.completed"]
        assert server_rows[0]["user_hash"] != USER_HASH


@pytest.mark.django_db
class TestOfflineBuffer:
    def test_a_restored_buffer_of_twenty_is_accepted(self, api_client):
        """`maxSize` defaults to 20 in the facade; a restored offline queue
        arrives as one batch of exactly that."""
        now_ms = int(timezone.now().timestamp() * 1000)
        batch = {
            "events": [
                {
                    "id": f"{now_ms}-{i}",
                    "kind": "track",
                    "name": "listing.published",
                    "props": {"listing_id": str(i)},
                    "ts": now_ms - i * 1000,
                }
                for i in range(20)
            ]
        }
        response = api_client.post(CANON_PATH, batch, format="json")
        assert response.json()["accepted"] == 20

    def test_one_stale_event_does_not_condemn_the_buffer(self, api_client):
        """The facade retries a batch until it drops ALL of it. Partial
        acceptance is what keeps nineteen good events."""
        now_ms = int(timezone.now().timestamp() * 1000)
        stale = int((timezone.now() - timedelta(days=30)).timestamp() * 1000)
        batch = {
            "events": [
                {"id": "a", "kind": "track", "name": "listing.published", "ts": now_ms},
                {"id": "b", "kind": "track", "name": "listing.published", "ts": stale},
            ]
        }
        body = api_client.post(CANON_PATH, batch, format="json").json()
        assert body["accepted"] == 1
        assert body["rejected"][0]["reason"] == "too_old"
