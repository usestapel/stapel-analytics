"""System checks: every one describes a configuration that LOOKS fine."""
import pytest
from django.core import checks as django_checks

from stapel_analytics import checks


def ids(results):
    return {result.id for result in results}


def run(check):
    return check(None)


class TestBaseline:
    def test_the_suite_configuration_only_trips_the_registry_warning(self, settings):
        """The harness declares no project vocabulary, so W001 is expected —
        and nothing else may fire on a correct default deployment."""
        every = []
        for name in checks.__all__:
            every.extend(run(getattr(checks, name)))
        assert ids(every) == {"analytics.W001"}

    def test_every_check_is_registered_with_django(self):
        registered = {
            getattr(check, "__name__", "") for check in django_checks.registry.registry.get_checks()
        }
        for name in checks.__all__:
            assert name in registered


class TestEventStoreInstalled:
    def test_it_passes_when_the_app_is_installed(self):
        assert run(checks.check_event_store_installed) == []

    def test_it_errors_when_the_app_is_missing(self, settings):
        settings.INSTALLED_APPS = [
            app for app in settings.INSTALLED_APPS
            if app != "stapel_core.django.eventstore"
        ]
        assert ids(run(checks.check_event_store_installed)) == {"analytics.E001"}

    def test_a_routed_stream_is_not_this_checks_business(self, settings):
        settings.INSTALLED_APPS = [
            app for app in settings.INSTALLED_APPS
            if app != "stapel_core.django.eventstore"
        ]
        settings.STAPEL_EVENTSTORE = {
            "ROUTES": {"analytics": "stapel_analytics.tests.test_checks.FakeStore"},
            "BUFFER_SYNC": True,
        }
        assert run(checks.check_event_store_installed) == []


class FakeStore:
    """A third-party backend: not the default Postgres one."""

    def append_batch(self, events):
        pass

    def query(self, *args, **kwargs):
        from stapel_core.eventstore.base import EventPage

        return EventPage(events=[], cursor=None)

    def rollup(self, *args, **kwargs):
        return []

    def purge(self, *args, **kwargs):
        return 0


# The seam wants an EventStore; declare it so resolve_backend accepts it.
from stapel_core.eventstore.base import EventStore  # noqa: E402

EventStore.register(FakeStore)


class TestEventsFile:
    def test_it_passes_without_the_setting(self):
        assert run(checks.check_events_file) == []

    def test_a_missing_file_is_an_error(self, settings):
        settings.STAPEL_ANALYTICS = {"EVENTS_FILE": "/nonexistent/events.json"}
        assert ids(run(checks.check_events_file)) == {"analytics.E002"}

    def test_a_readable_file_passes(self, settings, tmp_path):
        path = tmp_path / "events.json"
        path.write_text('[{"name": "a.b"}]', encoding="utf-8")
        settings.STAPEL_ANALYTICS = {"EVENTS_FILE": str(path)}
        assert run(checks.check_events_file) == []


class TestRegistryChecks:
    def test_an_empty_registry_warns(self):
        assert ids(run(checks.check_registry_declared)) == {"analytics.W001"}

    def test_a_declared_registry_is_silent(self, settings):
        settings.STAPEL_ANALYTICS = {"EVENTS": {"a.b": {"description": "x"}}}
        assert run(checks.check_registry_declared) == []

    def test_mode_off_silences_the_registry_warning(self, settings):
        settings.STAPEL_ANALYTICS = {"REGISTRY_MODE": "off"}
        assert run(checks.check_registry_declared) == []

    def test_mode_off_warns_on_its_own(self, settings):
        settings.STAPEL_ANALYTICS = {"REGISTRY_MODE": "off"}
        assert ids(run(checks.check_registry_mode)) == {"analytics.W003"}

    def test_the_default_mode_is_silent(self):
        assert run(checks.check_registry_mode) == []


class TestPiiCheck:
    def test_the_default_is_silent(self):
        assert run(checks.check_pii_guard) == []

    def test_strip_is_silent(self, settings):
        settings.STAPEL_ANALYTICS = {"PII_MODE": "strip"}
        assert run(checks.check_pii_guard) == []

    def test_off_warns(self, settings):
        settings.STAPEL_ANALYTICS = {"PII_MODE": "off"}
        assert ids(run(checks.check_pii_guard)) == {"analytics.W002"}


@pytest.mark.django_db
class TestFunnelStepsCheck:
    def test_a_declared_funnel_with_unknown_steps_warns(self, settings):
        settings.STAPEL_ANALYTICS = {
            "EVENTS": {"a.b": {"description": "x"}},
            "FUNNELS": {"sell": {"steps": ["a.b", "nobody.declared"]}},
        }
        results = run(checks.check_funnel_steps)
        assert ids(results) == {"analytics.W004"}
        assert "nobody.declared" in results[0].msg

    def test_a_declared_funnel_with_known_steps_is_silent(self, settings):
        settings.STAPEL_ANALYTICS = {
            "EVENTS": {"a.b": {"description": "x"}, "c.d": {"description": "y"}},
            "FUNNELS": {"sell": {"steps": ["a.b", "c.d"]}},
        }
        assert run(checks.check_funnel_steps) == []

    def test_an_authored_funnel_with_unknown_steps_warns(self, settings):
        from stapel_analytics.models import Funnel

        settings.STAPEL_ANALYTICS = {"EVENTS": {"a.b": {"description": "x"}}}
        Funnel.objects.create(slug="c", steps=["a.b", "nobody.declared"])
        assert ids(run(checks.check_funnel_steps)) == {"analytics.W004"}

    def test_an_inactive_funnel_is_not_judged(self, settings):
        from stapel_analytics.models import Funnel

        settings.STAPEL_ANALYTICS = {"EVENTS": {"a.b": {"description": "x"}}}
        Funnel.objects.create(slug="c", steps=["nobody"], is_active=False)
        assert run(checks.check_funnel_steps) == []

    def test_mode_off_disables_the_check(self, settings):
        settings.STAPEL_ANALYTICS = {
            "REGISTRY_MODE": "off",
            "FUNNELS": {"sell": {"steps": ["nobody"]}},
        }
        assert run(checks.check_funnel_steps) == []


class TestRetentionCheck:
    def test_the_default_horizon_is_silent(self):
        assert run(checks.check_retention) == []

    def test_no_horizon_at_all_warns(self, settings):
        settings.STAPEL_ANALYTICS = {"RETENTION_DAYS": None}
        assert ids(run(checks.check_retention)) == {"analytics.W005"}

    def test_an_event_store_retention_satisfies_it(self, settings):
        settings.STAPEL_ANALYTICS = {"RETENTION_DAYS": None}
        settings.STAPEL_EVENTSTORE = {"RETENTION": {"analytics": 90},
                                      "BUFFER_SYNC": True}
        assert run(checks.check_retention) == []


class TestAdapterCheck:
    def test_the_disabled_builtins_are_silent(self):
        assert run(checks.check_adapters) == []

    def test_an_enabled_webhook_without_a_url_warns(self, settings):
        settings.STAPEL_ANALYTICS = {"ADAPTERS": {"webhook": {"enabled": True}}}
        results = run(checks.check_adapters)
        assert ids(results) == {"analytics.W006"}
        assert "config['url']" in results[0].msg

    def test_an_enabled_webhook_with_a_url_is_silent(self, settings):
        settings.STAPEL_ANALYTICS = {
            "ADAPTERS": {"webhook": {"enabled": True,
                                     "config": {"url": "https://x.example"}}}
        }
        assert run(checks.check_adapters) == []

    def test_an_unimportable_handler_warns(self, settings):
        settings.STAPEL_ANALYTICS = {
            "ADAPTERS": {"x": {"handler": "no.such.module", "enabled": True}}
        }
        assert ids(run(checks.check_adapters)) == {"analytics.W006"}

    def test_a_handlerless_adapter_warns(self, settings):
        settings.STAPEL_ANALYTICS = {"ADAPTERS": {"x": {"enabled": True}}}
        assert ids(run(checks.check_adapters)) == {"analytics.W006"}


class TestBridgeCheck:
    def test_no_bridge_is_silent(self):
        assert run(checks.check_bridge_targets) == []

    def test_an_unregistered_bridge_target_warns(self, settings):
        from stapel_analytics.actions import reset_comm_bridge, wire_comm_bridge

        settings.STAPEL_ANALYTICS = {"COMM_BRIDGE": {"payment.completed": "paid"}}
        wire_comm_bridge()
        try:
            assert ids(run(checks.check_bridge_targets)) == {"analytics.W007"}
        finally:
            reset_comm_bridge()

    def test_a_registered_bridge_target_is_silent(self, settings):
        from stapel_analytics.actions import reset_comm_bridge, wire_comm_bridge

        settings.STAPEL_ANALYTICS = {
            "EVENTS": {"paid": {"description": "x"}},
            "COMM_BRIDGE": {"payment.completed": "paid"},
        }
        wire_comm_bridge()
        try:
            assert run(checks.check_bridge_targets) == []
        finally:
            reset_comm_bridge()


class TestSaltCheck:
    def test_no_salt_is_silent(self):
        assert run(checks.check_user_hash_salt) == []

    def test_a_salt_warns_about_the_join(self, settings):
        settings.STAPEL_ANALYTICS = {"USER_HASH_SALT": "pepper"}
        results = run(checks.check_user_hash_salt)
        assert ids(results) == {"analytics.W008"}
        assert "@stapel/analytics" in results[0].msg


class TestGdprOwnerCheck:
    def test_no_stapel_gdpr_means_nothing_to_warn_about(self):
        assert run(checks.check_gdpr_owner_declared) == []

    def test_an_undeclared_owner_warns(self, settings):
        settings.STAPEL_GDPR = {"DATA_OWNERS": ["profiles"]}
        assert ids(run(checks.check_gdpr_owner_declared)) == {"analytics.W009"}

    def test_a_declared_owner_is_silent(self, settings):
        settings.STAPEL_GDPR = {"DATA_OWNERS": ["profiles", "analytics"]}
        assert run(checks.check_gdpr_owner_declared) == []

    def test_the_dict_form_of_a_data_owner_is_understood(self, settings):
        settings.STAPEL_GDPR = {"DATA_OWNERS": [{"name": "analytics"}]}
        assert run(checks.check_gdpr_owner_declared) == []


class TestWriteKeyCheck:
    def test_the_default_is_silent(self):
        assert run(checks.check_write_keys) == []

    def test_required_with_no_keys_warns(self, settings):
        settings.STAPEL_ANALYTICS = {"REQUIRE_WRITE_KEY": True}
        assert ids(run(checks.check_write_keys)) == {"analytics.W010"}

    def test_required_with_keys_is_silent(self, settings):
        settings.STAPEL_ANALYTICS = {"REQUIRE_WRITE_KEY": True,
                                     "WRITE_KEYS": {"k": "web"}}
        assert run(checks.check_write_keys) == []


class TestDatabaseSafety:
    def test_the_db_reading_check_survives_an_unreachable_database(self):
        """Checks run before migrations — and before a database exists.

        No ``django_db`` mark here on purpose: pytest-django makes any ORM
        access raise, which is a faithful stand-in for "there is no table
        yet". A check that let that through would replace a useful warning
        with a broken boot.
        """
        assert checks._authored_funnel_steps() == []

    def test_the_funnel_check_is_silent_when_the_database_is_unreachable(self):
        assert checks.check_funnel_steps(None) == []
