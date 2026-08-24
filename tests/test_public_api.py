"""Package-level public API (PEP 562 lazy exports) and import hygiene."""
import os
import subprocess
import sys

import stapel_analytics


class TestLazyExports:
    def test_all_declares_public_api(self):
        assert stapel_analytics.__all__ == [
            "analytics_settings",
            "event_registry",
            "funnel_report",
            "register_adapter",
            "register_event",
            "track",
        ]

    def test_settings_resolve(self):
        from stapel_analytics.conf import analytics_settings

        assert stapel_analytics.analytics_settings is analytics_settings

    def test_track_resolves_to_the_service(self):
        from stapel_analytics.services import track

        assert stapel_analytics.track is track

    def test_event_registry_resolves(self):
        from stapel_analytics.registry import event_registry

        assert stapel_analytics.event_registry is event_registry

    def test_register_event_resolves(self):
        from stapel_analytics.registry import register_event

        assert stapel_analytics.register_event is register_event

    def test_register_adapter_resolves(self):
        from stapel_analytics.adapters import register_adapter

        assert stapel_analytics.register_adapter is register_adapter

    def test_funnel_report_resolves(self):
        from stapel_analytics.funnels import funnel_report

        assert stapel_analytics.funnel_report is funnel_report

    def test_dir_lists_the_public_api(self):
        assert set(stapel_analytics.__all__) <= set(dir(stapel_analytics))

    def test_unknown_attribute_raises(self):
        try:
            stapel_analytics.nonexistent_export
        except AttributeError as exc:
            assert "nonexistent_export" in str(exc)
        else:
            raise AssertionError("expected AttributeError")


class TestImportWithoutDjangoSettings:
    def test_package_import_is_django_free(self):
        """`import stapel_analytics` must not import Django nor require settings."""
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        code = (
            "import sys\n"
            "import stapel_analytics\n"
            'polluted = [m for m in sys.modules if m == "django" or m.startswith("django.")]\n'
            'assert not polluted, f"django imported at package import time: {polluted}"\n'
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(sys.executable),
        )
        assert result.returncode == 0, result.stderr

    def test_the_lazy_export_map_covers_all(self):
        assert set(stapel_analytics._LAZY_EXPORTS) == set(stapel_analytics.__all__)
