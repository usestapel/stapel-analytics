def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in
        # _codegen_settings.py so the test harness and any emission harness
        # can never drift.
        from stapel_analytics._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registries():
    """Runtime event definitions, runtime adapters and the events-file cache
    are process-global by design — reset them between tests so one test's
    registration never leaks into the next."""
    from stapel_analytics import adapters, registry

    yield
    registry.reset_events()
    adapters.reset_adapters()


@pytest.fixture(autouse=True)
def _flush_eventstore():
    """Persist buffered appends before a test's assertions and after it.

    ``BUFFER_SYNC`` is on in the suite settings, but an ``override_settings``
    block resets the store's singletons — the explicit flush keeps a test
    that overrides settings from reading a half-written stream.
    """
    yield
    from stapel_core import eventstore

    eventstore.flush()


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="owner", email="owner@example.com")


@pytest.fixture
def other_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(
        username="stranger", email="stranger@example.com"
    )


@pytest.fixture
def staff_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(
        username="operator", email="operator@example.com", is_staff=True
    )


@pytest.fixture
def authed_client(user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff_client(staff_user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def registry(settings):
    """Declare a small project vocabulary the way a host would.

    The suite installs only this module, so the built-ins are all the
    registry has out of the box; tests that fire named events declare them
    through the same door a project's events.json goes through.
    """

    def _declare(**events):
        declared = {
            name: {"description": f"test event {name}", "props": {}}
            for name in events or {}
        }
        declared.update({k: v for k, v in events.items() if isinstance(v, dict)})
        settings.STAPEL_ANALYTICS = {
            **getattr(settings, "STAPEL_ANALYTICS", {}),
            "EVENTS": declared,
        }
        return declared

    return _declare


@pytest.fixture
def recording_adapter():
    """Register an adapter that records instead of delivering."""
    from stapel_analytics.adapters import register_adapter

    calls = []

    def handler(events, config):
        calls.append({"events": list(events), "config": dict(config)})

    register_adapter("recorder", {"handler": handler, "enabled": True, "config": {}})
    return calls
