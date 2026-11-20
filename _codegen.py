"""stapel-analytics contract emission: the triad into ``docs/``.

From a single-module ``{analytics + core}`` Django instance mounted at the
canonical ``/analytics/api/v1/`` prefix:

  docs/schema.json     drf-spectacular OpenAPI (profile 1, what stapel-react reads)
  docs/schema.v2.json  contract profile 2 (what a native client generator reads)
  docs/flows.json      generate_flow_docs machine artifact
  docs/errors.json     generate_error_keys registry

The mechanism is ``stapel_tools.codegen``; this file wires the module's
settings and mount.

Usage:
    python -m stapel_analytics._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: The REST_FRAMEWORK a host runs under (stapel_core.django.settings), inlined
#: so emission reads no settings module: the authenticator and the schema
#: class decide ``security`` and the profile-2 annotations.
CODEGEN_REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "stapel_core.django.api.permissions.IsServiceRequest",
        "stapel_core.django.api.permissions.IsSuperUser",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
    "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
}

#: drf-spectacular derives the operationId prefix from the common path of all
#: endpoints; pin it to the monolith's "/" so operationIds keep the mount.
CODEGEN_SCHEMA_PATH_PREFIX = "/"


def _configure() -> None:
    """Configure and boot the single-module Django instance for emission."""
    from django.conf import settings

    if not settings.configured:
        from stapel_analytics._codegen_settings import settings_kwargs

        kwargs = settings_kwargs(root_urlconf="stapel_analytics.codegen_urls")
        kwargs["REST_FRAMEWORK"] = dict(CODEGEN_REST_FRAMEWORK)
        settings.configure(**kwargs)

    import django

    django.setup()

    from drf_spectacular.settings import spectacular_settings

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Component descriptions render differently per Python minor; the
    committed contracts are emitted on 3.12 (the CI pin)."""
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-analytics contract emission ABORTED: running Python {got}; "
            "contracts are emitted on Python 3.12."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()
    parser = argparse.ArgumentParser(
        prog="stapel-analytics-contract",
        description="Emit schema.json, schema.v2.json, flows.json and errors.json into --out.",
    )
    parser.add_argument("--out", default="docs", help="Output directory (default: docs).")
    parser.add_argument(
        "--check", action="store_true",
        help="Emit into a temporary directory and fail when --out differs.",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check(Path(args.out))
    return _emit(Path(args.out))


#: The files this harness writes, in emission order.
TRIAD = ("schema.json", "schema.v2.json", "flows.json", "errors.json")


def _check(committed: Path) -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _emit(Path(tmp))
        stale = [
            name for name in TRIAD
            if not (committed / name).is_file()
            or (committed / name).read_bytes() != (Path(tmp) / name).read_bytes()
        ]
    for name in stale:
        print(f"DRIFT: {committed}/{name} is stale — run `make contract` and commit it",
              file=sys.stderr)
    if not stale:
        print(f"contract-check: {committed}/{{{','.join(TRIAD)}}} up to date", file=sys.stderr)
    return 1 if stale else 0


def _emit(out: Path) -> int:
    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    # Profile 2 goes to its own file: docs/schema.json is what stapel-react's
    # generators read, and stays profile 1.
    emit_schema(out / "schema.v2.json", contract_profile=2)
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")
    print(
        f"stapel-analytics contract: {paths} paths, {flows} flows, {errors} error keys -> {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
