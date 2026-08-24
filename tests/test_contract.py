"""Contract tests: the documents and the code cannot drift.

A module whose MODULE.md, CONFIG.MD and settings disagree is a module an
integrator debugs by reading its source, which is exactly what those
documents exist to prevent.
"""
import json
import pathlib
import re

import pytest

from stapel_analytics.conf import DEFAULTS, analytics_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestSettingsNamespace:
    def test_the_namespace_is_stapel_analytics(self):
        assert analytics_settings.namespace == "STAPEL_ANALYTICS"

    def test_defaults_is_the_literal_the_appsettings_uses(self):
        assert analytics_settings.defaults == DEFAULTS

    def test_every_default_is_documented_in_module_md(self):
        module_md = read("MODULE.md")
        missing = [key for key in DEFAULTS if key not in module_md]
        assert missing == []

    def test_every_documented_setting_exists(self):
        """A settings table naming a key the code does not read is worse than
        no table: it is a promise the module never made."""
        module_md = read("MODULE.md")
        documented = set(re.findall(r"^\| `([A-Z_]+)`", module_md, re.MULTILINE))
        assert documented - set(DEFAULTS) == set()

    def test_code_naming_keys_are_import_strings(self):
        """A name that decides which code runs is never read from the env."""
        assert analytics_settings.import_strings == frozenset(
            {"SUBJECT_RESOLVER", "PII_GUARD"}
        )

    def test_the_import_string_defaults_resolve(self):
        assert callable(analytics_settings.SUBJECT_RESOLVER)
        assert callable(analytics_settings.PII_GUARD)


class TestErrorKeys:
    def test_every_key_is_owned_by_this_module(self):
        from stapel_analytics.errors import (
            STAPEL_ANALYTICS_ERRORS,
            STAPEL_ANALYTICS_EVENT_ERRORS,
        )

        for key in {**STAPEL_ANALYTICS_ERRORS, **STAPEL_ANALYTICS_EVENT_ERRORS}:
            assert re.match(r"^error\.\d{3}\.analytics_", key), key

    def test_every_key_carries_a_remediation(self):
        from stapel_analytics.errors import (
            STAPEL_ANALYTICS_ERRORS,
            STAPEL_ANALYTICS_REMEDIATION,
        )

        assert set(STAPEL_ANALYTICS_ERRORS) == set(STAPEL_ANALYTICS_REMEDIATION)

    def test_every_rejection_reason_maps_to_a_key(self):
        """A client that shows "3 rejected" without saying why is a client
        whose user reports "analytics is broken"."""
        from stapel_analytics.errors import REJECTION_KEYS, STAPEL_ANALYTICS_EVENT_ERRORS

        assert set(REJECTION_KEYS.values()) <= set(STAPEL_ANALYTICS_EVENT_ERRORS)

    def test_every_reason_the_ingest_produces_has_a_key(self):
        from stapel_analytics.errors import REJECTION_KEYS

        source = read("ingest.py")
        reasons = set(re.findall(r'Rejection\([^,]+,[^,]+,\s*"([a-z_]+)"', source))
        assert reasons <= set(REJECTION_KEYS), reasons - set(REJECTION_KEYS)

    def test_every_batch_refusal_maps_to_a_key(self):
        from stapel_analytics.errors import BATCH_KEYS

        source = read("ingest.py") + read("services.py")
        keys = set(re.findall(r'IngestRefused\("([a-z_]+)"', source))
        assert keys <= set(BATCH_KEYS), keys - set(BATCH_KEYS)

    def test_the_translation_catalogues_cover_every_key(self):
        """Owning keys means shipping their catalogues."""
        from stapel_analytics.errors import (
            STAPEL_ANALYTICS_ERRORS,
            STAPEL_ANALYTICS_EVENT_ERRORS,
        )

        owned = set(STAPEL_ANALYTICS_ERRORS) | set(STAPEL_ANALYTICS_EVENT_ERRORS)
        for path in (ROOT / "translations").glob("errors.*.json"):
            catalogue = json.loads(path.read_text(encoding="utf-8"))
            assert set(catalogue) == owned, path.name

    def test_at_least_the_fleet_languages_ship(self):
        names = {p.name for p in (ROOT / "translations").glob("errors.*.json")}
        assert {"errors.ru.json", "errors.es.json"} <= names


class TestSchemas:
    def test_every_committed_schema_is_valid_json(self):
        for path in (ROOT / "schemas").rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_every_schema_titles_itself_after_its_filename(self):
        for path in (ROOT / "schemas").rglob("*.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            assert schema["title"] == path.stem, path

    def test_every_schema_describes_itself(self):
        for path in (ROOT / "schemas").rglob("*.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            assert schema.get("description"), path

    def test_the_emitted_topic_constant_matches_its_schema(self):
        from stapel_analytics.events import EVENTS_RECORDED

        assert (ROOT / "schemas" / "emits" / f"{EVENTS_RECORDED}.json").exists()

    def test_consumes_are_documentation_not_registration(self):
        """`autoload_schemas` walks emits/ and functions/ only."""
        assert (ROOT / "schemas" / "consumes" / "README.md").exists()


class TestPackaging:
    def test_package_data_patterns_match_real_files(self):
        """A package-data entry matching nothing ships a wheel that silently
        holds less than the repo."""
        import tomllib

        with open(ROOT / "pyproject.toml", "rb") as handle:
            config = tomllib.load(handle)
        patterns = config["tool"]["setuptools"]["package-data"]["stapel_analytics"]
        for pattern in patterns:
            assert list(ROOT.glob(pattern)), pattern

    def test_the_migrations_package_is_declared(self):
        import tomllib

        with open(ROOT / "pyproject.toml", "rb") as handle:
            config = tomllib.load(handle)
        assert "stapel_analytics.migrations" in (
            config["tool"]["setuptools"]["packages"]
        )

    def test_the_core_floor_is_declared(self):
        import tomllib

        with open(ROOT / "pyproject.toml", "rb") as handle:
            config = tomllib.load(handle)
        deps = config["project"]["dependencies"]
        assert any(dep.startswith("stapel-core>=") for dep in deps)


class TestUrlSurface:
    def test_every_v1_route_is_named(self):
        from stapel_analytics import urls_v1

        for pattern in urls_v1.urlpatterns:
            assert pattern.name, pattern

    def test_the_gate_registry_covers_every_route(self):
        from stapel_analytics.urls_v1 import GATE_REGISTRY, urlpatterns

        gated = [p for entry in GATE_REGISTRY.values() for p in entry.patterns]
        assert len(gated) == len(urlpatterns)

    def test_the_ingest_route_is_its_own_gate(self):
        from stapel_analytics.urls_v1 import GATE_REGISTRY

        assert GATE_REGISTRY["analytics.ingest"].patterns[0].name == "analytics-ingest"

    def test_the_module_bakes_in_the_version_segment(self):
        from stapel_analytics import urls

        assert str(urls.urlpatterns[0].pattern) == "api/v1/"


class TestDocumentation:
    @pytest.mark.parametrize(
        "name", ["README.md", "MODULE.md", "CHANGELOG.md", "CONFIG.MD", "codecov.yml"]
    )
    def test_the_house_documents_exist(self, name):
        assert (ROOT / name).is_file()

    def test_the_publish_workflow_ships(self):
        assert (ROOT / ".github" / "workflows" / "publish.yml").is_file()

    def test_the_ci_workflow_ships(self):
        assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()

    def test_module_md_names_every_check_id(self):
        from stapel_analytics import checks

        source = read("checks.py")
        ids = set(re.findall(r'id="(analytics\.[EW]\d+)"', source))
        module_md = read("MODULE.md")
        assert ids
        assert {i for i in ids if i not in module_md} == set()
        assert len(checks.__all__) == len(ids)

    def test_module_md_names_every_route(self):
        from stapel_analytics import urls_v1

        module_md = read("MODULE.md")
        for pattern in urls_v1.urlpatterns:
            route = str(pattern.pattern)
            assert route.split("/")[0].split("<")[0] in module_md, route

    def test_the_changelog_has_an_entry_for_the_current_version(self):
        import tomllib

        with open(ROOT / "pyproject.toml", "rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        assert version in read("CHANGELOG.md")

    def test_the_design_of_record_is_cited(self):
        assert "analytics-standard" in read("MODULE.md")


class TestPresenterDiscipline:
    def test_no_view_instantiates_a_dto(self):
        """SWAP002: a DTO is built by a presenter, never by a view."""
        import stapel_analytics.dto as dto_module

        source = read("views.py")
        names = [
            name for name in dir(dto_module)
            if name[:1].isupper() and not name.startswith("_")
        ]
        for name in names:
            assert f"{name}(" not in source, name

    def test_no_view_imports_a_concrete_presenter(self):
        """SWAP001: a direct import is what a STAPEL_SWAP override misses."""
        source = read("views.py")
        assert "Presenter," not in source.split("from .presenters import")[1].split(
            ")"
        )[0].replace("get_", "")

    def test_every_swap_key_is_declared(self):
        from stapel_analytics import presenters

        keys = [
            value for name, value in vars(presenters).items()
            if name.endswith("_PRESENTER_KEY")
        ]
        assert len(keys) == 4
        for key in keys:
            assert key.startswith("ANALYTICS_")
