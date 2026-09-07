"""``docs/errors.json`` codegen artifact + drift gate (contract-pipeline.md §2).

The language-agnostic registry of every ``error.<status>.<name>`` key this
service can raise, with its HTTP ``status``, ``{param}`` slots, machine-readable
``remediation`` hint, canonical English text and owning package. It is the
DENOMINATOR every consumer divides by: a host frontend that compiles the
fleet's error catalogs discovers a service's codes here and pairs them with
``translations/errors.<lang>.json``. Until 0.5.1 this module shipped the
catalogs and not the registry, so its 23 keys were invisible to such a consumer
even with their translations on disk beside them.

The committed file must be exactly what ``generate_error_keys`` emits from the
live error registry — the same byte-stable regenerate-and-diff discipline as
stapel-gdpr's and stapel-auth's gates.

Regenerate after adding/changing an error key or its remediation:

    STAPEL_REGEN_ERROR_KEYS=1 python -m pytest \
        tests/test_error_keys.py::test_error_keys_have_no_drift

then commit ``docs/errors.json``. Without the env var the same test is the CI
drift gate: it regenerates into a temp dir and asserts byte-for-byte equality
with the committed artifact (a no-op regen is a no-op diff).
"""
import io
import json
import os
import re
from pathlib import Path

from django.core.management import call_command
from stapel_core.django.api.errors import REMEDIATION_VOCAB

REPO = Path(__file__).resolve().parent.parent
ERRORS_JSON = REPO / "docs" / "errors.json"
TRANSLATIONS = REPO / "translations"


def _owned() -> dict[str, str]:
    from stapel_analytics.errors import (
        STAPEL_ANALYTICS_ERRORS,
        STAPEL_ANALYTICS_EVENT_ERRORS,
    )

    return {**STAPEL_ANALYTICS_ERRORS, **STAPEL_ANALYTICS_EVENT_ERRORS}


def _generate(out: Path) -> None:
    call_command("generate_error_keys", "--out", str(out), stdout=io.StringIO())


def test_error_keys_have_no_drift(tmp_path):
    if os.environ.get("STAPEL_REGEN_ERROR_KEYS"):
        _generate(ERRORS_JSON)
        return

    out = tmp_path / "errors.json"
    _generate(out)
    assert ERRORS_JSON.read_bytes() == out.read_bytes(), (
        "errors.json drifted — run "
        "STAPEL_REGEN_ERROR_KEYS=1 pytest tests/test_error_keys.py and commit "
        "docs/errors.json"
    )


def test_committed_artifact_shape():
    entries = json.loads(ERRORS_JSON.read_text())
    assert isinstance(entries, list) and entries
    codes = [e["code"] for e in entries]
    assert codes == sorted(codes), "entries must be sorted by code"
    assert len(codes) == len(set(codes)), "codes must be unique"
    for e in entries:
        assert set(e) == {"code", "status", "params", "remediation", "en", "owner"}
        assert e["code"].startswith("error.")
        assert e["status"] == int(e["code"].split(".")[1])
        assert isinstance(e["params"], list)
        assert e["remediation"] in REMEDIATION_VOCAB
        assert e["en"] and isinstance(e["en"], str)
        assert e["owner"] is None or isinstance(e["owner"], str)
        # Every `{param}` slot in the text is declared in params.
        slots = {m.group(1) for m in re.finditer(r"\{(\w+)\}", e["en"])}
        assert slots <= set(e["params"])


def test_registry_declares_every_key_this_module_owns():
    """Every owned key, with this package as owner and the registry literal as text."""
    entries = {e["code"]: e for e in json.loads(ERRORS_JSON.read_text())}
    owned = _owned()
    for code, text in owned.items():
        assert code in entries, f"{code} missing from docs/errors.json"
        assert entries[code]["owner"] == "stapel_analytics", code
        assert entries[code]["en"] == text, code
    assert len([c for c in entries if ".analytics_" in c]) == len(owned)


def test_registry_declares_every_key_this_module_translates():
    """The two artifacts are two halves of one contract.

    ``translations/errors.<lang>.json`` is keyed by code; a consumer keeps only
    the codes the registry declares. A translated key with no registry entry is
    a string nobody can ever render.
    """
    declared = {e["code"] for e in json.loads(ERRORS_JSON.read_text())}
    for path in sorted(TRANSLATIONS.glob("errors.*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        undeclared = sorted(k for k in catalog if k not in declared)
        assert not undeclared, (
            f"{path.name} translates {len(undeclared)} code(s) absent from "
            f"docs/errors.json: {undeclared[:8]}"
        )


def test_translations_preserve_placeholders():
    """Every localized text keeps exactly the canon's ``{param}`` slots."""
    from stapel_core.i18n.domains import params_of

    owned = _owned()
    for path in sorted(TRANSLATIONS.glob("errors.*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for key, text in catalog.items():
            assert set(params_of(text)) == set(params_of(owned[key])), (
                f"{path.name}: {key}"
            )
