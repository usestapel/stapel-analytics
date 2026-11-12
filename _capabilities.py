"""stapel-analytics capabilities.json emitter.

This module has no OpenAPI triad yet, so the document is built the way
stapel-core's is (:func:`stapel_tools.surface.build_static_capabilities`:
provides / extension_points / requires from the curated
``docs/capabilities.meta.json``, ``surface`` derived from its
``surface_roots`` by AST) — with one addition: the axes are not hand-listed.
``key`` / ``kind`` / ``default`` are read from ``conf.DEFAULTS`` for the keys
in :data:`AXES` (kind derived with :func:`stapel_tools.capabilities.axis_kind`,
the full emitter's rule), and only the prose is curated. A key selected here
without prose in the meta layer, or prose for a key not selected, fails
loudly.

    python -m stapel_analytics._capabilities [--check]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stapel_tools.capabilities import axis_kind
from stapel_tools.surface import _stable_json, build_static_capabilities, load_meta

REPO = Path(__file__).resolve().parent

#: The decisions a product owner makes here, with their group. Caps, schedules,
#: page sizes and the Google Ads credentials are tuning or secrets, not axes;
#: the registries and dotted paths are extension points.
AXES = {
    "REGISTRY_MODE": "analytics.ingest",
    "REQUIRE_WRITE_KEY": "analytics.ingest",
    "PII_MODE": "analytics.privacy",
    "RETENTION_DAYS": "analytics.privacy",
    "FANOUT_ENABLED": "analytics.fanout",
}


def derived_axes(meta: dict) -> list[dict]:
    from stapel_analytics.conf import DEFAULTS

    curated = meta.get("axes") or {}
    missing = [k for k in AXES if k not in curated]
    extra = [k for k in curated if k not in AXES]
    if missing or extra:
        raise SystemExit(
            "capabilities: curated axes out of sync with _capabilities.AXES — "
            f"missing prose: {missing or 'none'}; stale prose: {extra or 'none'}"
        )
    axes = []
    for key, group in AXES.items():
        if key not in DEFAULTS:
            raise SystemExit(f"capabilities: axis {key!r} is not a conf.DEFAULTS key")
        prose = curated[key]
        for field in ("summary", "business_label"):
            if not prose.get(field):
                raise SystemExit(f"capabilities: axis {key!r} has no curated {field!r}")
        axes.append(
            {
                "key": key,
                "kind": axis_kind(DEFAULTS[key]),
                "default": DEFAULTS[key],
                "group": group,
                "curated": {
                    "summary": prose["summary"],
                    "business_label": prose["business_label"],
                },
            }
        )
    return axes


def build(repo: Path = REPO) -> dict:
    meta = load_meta(repo)
    return build_static_capabilities(repo, {**meta, "axes": derived_axes(meta)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stapel-analytics-capabilities")
    parser.add_argument("--check", action="store_true", help="fail on drift, write nothing")
    args = parser.parse_args(argv)
    rendered = _stable_json(build())
    target = REPO / "docs" / "capabilities.json"
    if args.check:
        if not target.is_file() or target.read_text() != rendered:
            print(f"DRIFT: {target} is stale — run `make contract` and commit it", file=sys.stderr)
            return 1
        print(f"capabilities: {target} is up to date", file=sys.stderr)
        return 0
    target.write_text(rendered)
    print(f"stapel-analytics capabilities → {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
