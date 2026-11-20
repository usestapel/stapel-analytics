"""A batch-level ``context`` describes the emitter and lands on every event.

A native collector has no per-event place for platform, app version, OS or
locale short of stuffing ``props`` of every event; the batch carries them
once and the store keeps them per row, where reports and fan-out read them.
"""
import pytest
from django.utils import timezone

from stapel_analytics import store

INGEST = "/analytics/api/v1/events"

CONTEXT = {
    "platform": "ios",
    "app_version": "1.4.0",
    "os_version": "18.1",
    "locale": "en-US",
    "sdk": "stapel-analytics-collector/0.1.0",
}


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = {"EVENTS": {"app.opened": {"description": "x", "flow": "f"}}}


def _event(**overrides):
    base = {"kind": "track", "name": "app.opened", "props": {},
            "ts": int(timezone.now().timestamp() * 1000)}
    base.update(overrides)
    return base


def _rows():
    return list(store.iter_events())


@pytest.mark.django_db
def test_the_batch_context_is_stored_on_every_event(api_client):
    response = api_client.post(
        INGEST, {"events": [_event(), _event(id="e2")], "context": CONTEXT}, format="json"
    )
    assert response.status_code == 202, response.content
    assert response.json()["accepted"] == 2
    rows = _rows()
    assert len(rows) == 2
    assert [row["context"] for row in rows] == [CONTEXT, CONTEXT]


@pytest.mark.django_db
def test_unknown_and_empty_context_keys_are_not_stored(api_client):
    response = api_client.post(
        INGEST,
        {"events": [_event()], "context": {"platform": "android", "locale": "", "device": "x"}},
        format="json",
    )
    assert response.status_code == 202
    assert _rows()[0]["context"] == {"platform": "android"}


@pytest.mark.django_db
def test_inversion_no_context_stores_no_key(api_client):
    api_client.post(INGEST, {"events": [_event()]}, format="json")
    assert "context" not in _rows()[0]


@pytest.mark.django_db
def test_a_context_that_is_not_an_object_refuses_the_batch(api_client):
    response = api_client.post(
        INGEST, {"events": [_event()], "context": "ios"}, format="json"
    )
    assert response.status_code == 400
    assert response.json()["localizable_error"] == "error.400.analytics_batch_shape"
    assert _rows() == []
