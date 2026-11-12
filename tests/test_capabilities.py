"""Drift gate for docs/capabilities.json (built by stapel_analytics._capabilities)."""
import json
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401
except ImportError as exc:  # pragma: no cover - a skipped drift gate is no gate
    raise RuntimeError(
        "the capabilities drift gate needs stapel-tools (it carries the emitter)"
    ) from exc

from stapel_tools.surface import _stable_json

from stapel_analytics import _capabilities
from stapel_analytics.conf import DEFAULTS

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "docs" / "capabilities.json"


def test_capabilities_committed_and_has_no_drift():
    assert COMMITTED.is_file(), "docs/capabilities.json is missing — run `make contract`"
    assert COMMITTED.read_text() == _stable_json(_capabilities.build()), (
        "docs/capabilities.json is stale — run `make contract` and commit it"
    )


def test_axes_are_read_from_conf_not_retyped():
    doc = json.loads(COMMITTED.read_text())
    axes = {a["key"]: a for a in doc["axes"]}
    assert list(axes) == list(_capabilities.AXES)
    for key, axis in axes.items():
        assert axis["default"] == DEFAULTS[key]
    assert axes["PII_MODE"]["default"] == "reject"


def test_version_matches_pyproject():
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    doc = json.loads(COMMITTED.read_text())
    assert (doc["module"], doc["version"]) == (project["name"], project["version"])


def test_curated_prose_out_of_sync_fails_loudly():
    meta = json.loads((REPO / "docs" / "capabilities.meta.json").read_text())
    broken = json.loads(json.dumps(meta))
    del broken["axes"]["PII_MODE"]
    with pytest.raises(SystemExit, match="PII_MODE"):
        _capabilities.derived_axes(broken)
    broken = json.loads(json.dumps(meta))
    broken["axes"]["NO_SUCH_AXIS"] = {"summary": "x", "business_label": "x"}
    with pytest.raises(SystemExit, match="NO_SUCH_AXIS"):
        _capabilities.derived_axes(broken)


def test_surface_is_derived_and_every_entry_has_intent():
    doc = json.loads(COMMITTED.read_text())
    names = {e["name"] for e in doc["surface"]}
    assert {"track", "guard_props", "hash_user_id"} <= names
    assert all(e.get("intent") for e in doc["surface"])
