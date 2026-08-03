"""Command line interface (tasks 6.1, 6.2, 6.4, 6.5).

    recall-memory-mcp init            create the local config + authority DB
    recall-memory-mcp doctor          diagnose this installation (redacted)
    recall-memory-mcp client-config   print (or --write) a host configuration

Design rules that the tests pin down:

* ``main`` takes an explicit environment mapping and explicit streams, so the
  test-suite can run it against an isolated HOME/TEMP without ever reading the
  operator's real configuration.
* ``init`` shows both target paths *before* creating anything and refuses to
  overwrite either of them (R9.2).
* ``doctor`` output is redacted: no absolute paths, no secrets, no traceback
  (R8.5 / R9.4).  Paths are legitimate in ``init`` — the local operator has to
  know what is about to be created — but never in diagnostics.
* ``client-config`` prints a template by default; ``--write`` is the only way
  to touch a host file and always backs up + reads back first (R9.5).

``serve`` is deliberately absent until the authority wiring it needs
(concrete repository/embedder adapter + request-scoped context provider)
exists; shipping a console command that cannot serve would be a lie.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TextIO

from . import MCP_SDK_REQUIREMENT, __version__, client_configs, provisioning, redaction
from .settings import LOOPBACK_HOSTS, ServerMode, ServerSettings, SettingsError

PROG: Final[str] = "recall-memory-mcp"

EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_REFUSED: Final[int] = 3

STATUS_OK: Final[str] = "ok"
STATUS_WARN: Final[str] = "warn"
STATUS_FAIL: Final[str] = "fail"


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def resolve_settings(env: Mapping[str, str]) -> ServerSettings:
    """Validated settings from an explicit environment mapping."""

    return ServerSettings.from_env(dict(env))


def _env_with_overrides(
    env: Mapping[str, str], *, config_dir: str | None = None, db_path: str | None = None
) -> dict[str, str]:
    merged = dict(env)
    if config_dir:
        merged["RECALL_MCP_CONFIG_DIR"] = config_dir
    if db_path:
        merged["RECALL_MCP_DB_PATH"] = db_path
    return merged


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[Check, ...]

    @property
    def status(self) -> str:
        if any(check.status == STATUS_FAIL for check in self.checks):
            return STATUS_FAIL
        if any(check.status == STATUS_WARN for check in self.checks):
            return STATUS_WARN
        return STATUS_OK

    @property
    def exit_code(self) -> int:
        return EXIT_FAILURE if self.status == STATUS_FAIL else EXIT_OK

    def redacted_payload(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "summary": check.summary,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
        }
        return redaction.redact_mapping(payload)


def _installed_version(distribution: str) -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

        return version(distribution)
    except Exception:  # noqa: BLE001 - a missing distribution is a normal answer
        return None


def _check_package() -> Check:
    return Check(
        name="package",
        status=STATUS_OK,
        summary=f"recall-memory-mcp {__version__}",
        detail={"version": __version__, "python": ".".join(str(p) for p in sys.version_info[:3])},
    )


def _check_sdk() -> Check:
    installed = _installed_version("mcp")
    if installed is None:
        return Check(
            name="sdk",
            status=STATUS_FAIL,
            summary="the official MCP SDK is not installed",
            detail={"requirement": MCP_SDK_REQUIREMENT, "installed": None},
        )
    major_minor = tuple(int(part) for part in installed.split(".")[:2] if part.isdigit())
    compatible = major_minor[:2] == (2, 0)
    return Check(
        name="sdk",
        status=STATUS_OK if compatible else STATUS_FAIL,
        summary=f"mcp {installed} ({MCP_SDK_REQUIREMENT})",
        detail={
            "requirement": MCP_SDK_REQUIREMENT,
            "installed": installed,
            "compatible": compatible,
        },
    )


def _check_config(settings: ServerSettings) -> Check:
    path = settings.config_path
    if not path.is_file():
        return Check(
            name="config",
            status=STATUS_FAIL,
            summary=f"no configuration file; run `{PROG} init` first",
            detail={"exists": False},
        )
    try:
        document = provisioning.read_config(path)
    except Exception as exc:  # noqa: BLE001
        return Check(
            name="config",
            status=STATUS_FAIL,
            summary="configuration file could not be parsed",
            detail={"exists": True, "parse_error": type(exc).__name__},
        )
    schema = document.get("schema")
    ok = schema == provisioning.CONFIG_SCHEMA_VERSION
    return Check(
        name="config",
        status=STATUS_OK if ok else STATUS_WARN,
        summary=f"configuration schema {schema}",
        detail={
            "exists": True,
            "schema": schema,
            "owner_recorded": bool(document.get("owner_id")),
        },
    )


def _check_database(settings: ServerSettings) -> Check:
    status = provisioning.database_status(settings.db_path)
    if not status.exists:
        return Check(
            name="database",
            status=STATUS_FAIL,
            summary=f"no authority database; run `{PROG} init` first",
            detail={
                "exists": False,
                "schema_version": 0,
                "integrity_ok": False,
                "missing_tables": list(status.missing_tables),
            },
        )
    detail = {
        "exists": True,
        "schema_version": status.schema_version,
        "integrity_ok": status.integrity_ok,
        "missing_tables": list(status.missing_tables),
        "error": status.error,
    }
    if not status.ready:
        return Check(
            name="database",
            status=STATUS_FAIL,
            summary="authority database failed verification",
            detail=detail,
        )
    return Check(
        name="database",
        status=STATUS_OK,
        summary=f"authority schema version {status.schema_version}, integrity ok",
        detail=detail,
    )


def _check_embedding() -> Check:
    try:
        from recall import embed as embed_module  # noqa: PLC0415 - deliberately lazy
    except Exception as exc:  # noqa: BLE001
        return Check(
            name="embedding",
            status=STATUS_WARN,
            summary="embedding backend unavailable; retrieval runs degraded",
            detail={"available": False, "loaded": False, "reason": type(exc).__name__},
        )
    try:
        loaded = bool(embed_module.is_loaded())
    except Exception:  # noqa: BLE001
        loaded = False
    return Check(
        name="embedding",
        status=STATUS_OK,
        summary="embedding backend importable",
        detail={"available": True, "loaded": loaded},
    )


def _check_auth(settings: ServerSettings) -> Check:
    detail = {
        "mode": settings.mode.value,
        "require_auth": settings.require_auth,
        "oauth_issuer_configured": bool(settings.oauth_issuer),
        "public_url_is_https": bool(
            settings.public_url and settings.public_url.lower().startswith("https://")
        ),
    }
    if settings.mode is ServerMode.LOCAL and not settings.require_auth:
        return Check(
            name="auth",
            status=STATUS_WARN,
            summary="local loopback mode without OAuth; do not expose this port",
            detail=detail,
        )
    if not settings.require_auth:
        return Check(
            name="auth",
            status=STATUS_FAIL,
            summary="non-local mode without authentication",
            detail=detail,
        )
    if settings.mode is ServerMode.REMOTE and not detail["public_url_is_https"]:
        return Check(
            name="auth",
            status=STATUS_FAIL,
            summary="remote mode without an https public URL",
            detail=detail,
        )
    return Check(
        name="auth",
        status=STATUS_OK,
        summary=f"{settings.mode.value} mode with authentication required",
        detail=detail,
    )


def _port_in_use(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in LOOPBACK_HOSTS else host
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.5)
        try:
            return probe.connect_ex((target, port)) == 0
        except OSError:
            return False


def _check_transport(settings: ServerSettings) -> Check:
    loopback_only = settings.host in LOOPBACK_HOSTS
    wildcard = any(
        "*" in entry for entry in settings.allowed_hosts + settings.allowed_origins
    )
    detail = {
        "mode": settings.mode.value,
        "loopback_only": loopback_only,
        "wildcard_allowlist": wildcard,
        "port": settings.port,
        "port_in_use": _port_in_use(settings.host, settings.port),
        "stateful_http": settings.stateful_http,
        "allowed_host_count": len(settings.allowed_hosts),
    }
    if wildcard:
        return Check(
            name="transport",
            status=STATUS_FAIL,
            summary="wildcard host/origin allowlist",
            detail=detail,
        )
    if settings.mode is ServerMode.LOCAL and not loopback_only:
        return Check(
            name="transport",
            status=STATUS_FAIL,
            summary="local mode is not bound to loopback",
            detail=detail,
        )
    return Check(
        name="transport",
        status=STATUS_OK,
        summary=f"{settings.mode.value} transport on port {settings.port}",
        detail=detail,
    )


def run_doctor(settings: ServerSettings) -> DoctorReport:
    return DoctorReport(
        checks=(
            _check_package(),
            _check_sdk(),
            _check_config(settings),
            _check_database(settings),
            _check_embedding(),
            _check_auth(settings),
            _check_transport(settings),
        )
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _fail(stderr: TextIO, message: str, code: int) -> int:
    stderr.write(f"error: {redaction.redact_text(message)}\n")
    return code


def _cmd_init(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    merged = _env_with_overrides(env, config_dir=args.config_dir, db_path=args.db_path)
    try:
        settings = resolve_settings(merged)
    except SettingsError as exc:
        return _fail(err, str(exc), EXIT_USAGE)

    # Task 6.2 — disclose both targets *before* creating anything.
    out.write(f"{PROG} init\n")
    out.write(f"  config file : {settings.config_path}\n")
    out.write(f"  database    : {settings.db_path}\n")
    out.write("Neither target may already exist; nothing is overwritten.\n")

    try:
        report = provisioning.initialize(settings)
    except provisioning.RefusedOverwriteError as exc:
        return _fail(err, str(exc), EXIT_REFUSED)
    except provisioning.AuthorityCoreUnavailableError as exc:
        return _fail(err, str(exc), EXIT_FAILURE)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback
        return _fail(err, redaction.redact_exception(exc), EXIT_FAILURE)

    out.write(f"created authority database (schema version {report.schema_version})\n")
    out.write("created config file\n")
    out.write(
        "read-back OK: reopened the authority database, "
        f"schema version {report.schema_version}\n"
    )
    out.write(f"owner id    : {report.owner_id}\n")
    out.write(
        "owner-only permissions: "
        f"config={'yes' if report.config_owner_only else 'no'} "
        f"database={'yes' if report.db_owner_only else 'no'}\n"
    )
    out.write(f"next: run `{PROG} doctor`\n")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    try:
        settings = resolve_settings(env)
    except SettingsError as exc:
        return _fail(err, str(exc), EXIT_FAILURE)

    report = run_doctor(settings)
    payload = report.redacted_payload()

    if args.json:
        out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return report.exit_code

    out.write(f"{PROG} doctor\n")
    for check in payload["checks"]:
        out.write(f"  [{check['status']:>4}] {check['name']:<10} {check['summary']}\n")
    out.write(f"overall: {payload['status']}\n")
    return report.exit_code


def _cmd_client_config(
    args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO
) -> int:
    try:
        settings = resolve_settings(env)
    except SettingsError as exc:
        return _fail(err, str(exc), EXIT_FAILURE)

    try:
        template = client_configs.render(args.client, settings)
    except client_configs.ClientConfigError as exc:
        return _fail(err, str(exc), EXIT_USAGE)

    if not args.write:
        # stdout stays pure template text so it can be piped straight into a
        # file or a JSON parser; operator hints go to stderr.
        out.write(template.text if template.text.endswith("\n") else template.text + "\n")
        if template.path_hint:
            err.write(f"target: {template.path_hint}\n")
        err.write("nothing was written; re-run with --write <path>\n")
        return EXIT_OK

    try:
        report = client_configs.write_host_config(args.client, settings, args.write)
    except client_configs.RefusedWriteError as exc:
        return _fail(err, str(exc), EXIT_REFUSED)
    except Exception as exc:  # noqa: BLE001
        return _fail(err, redaction.redact_exception(exc), EXIT_FAILURE)

    if report.backup_path is not None:
        out.write(f"backup      : {report.backup_path}\n")
    else:
        out.write("backup      : not needed (new file)\n")
    out.write(f"wrote       : {report.path}\n")
    out.write("read-back OK: the host configuration parses and matches the template\n")
    out.write(f"owner-only  : {'yes' if report.owner_only else 'no'}\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser / entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Cross-client shared memory over the Model Context Protocol.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    init = sub.add_parser("init", help="create the local config file and authority database")
    init.add_argument("--config-dir", default=None, help="directory for config.toml")
    init.add_argument("--db-path", default=None, help="path of the authority database")
    init.set_defaults(handler=_cmd_init)

    doctor = sub.add_parser("doctor", help="diagnose this installation (redacted output)")
    doctor.add_argument("--json", action="store_true", help="emit a machine-readable report")
    doctor.set_defaults(handler=_cmd_doctor)

    client = sub.add_parser("client-config", help="print a host configuration template")
    client.add_argument("client", choices=list(client_configs.CLIENTS))
    client.add_argument(
        "--write",
        default=None,
        metavar="PATH",
        help="write the configuration to PATH (backup + read-back first)",
    )
    client.set_defaults(handler=_cmd_client_config)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    import os

    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ) if env is None else dict(env)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_parser()
    buffer_out, buffer_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer_out), contextlib.redirect_stderr(buffer_err):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        out.write(buffer_out.getvalue())
        err.write(buffer_err.getvalue())
        code = exc.code
        if code is None or code == 0:
            return EXIT_OK
        return EXIT_USAGE
    finally:
        pass

    out.write(buffer_out.getvalue())
    err.write(buffer_err.getvalue())
    return int(args.handler(args, env, out, err))


def run() -> None:  # pragma: no cover - console-script shim
    raise SystemExit(main())


__all__ = [
    "Check",
    "DoctorReport",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "build_parser",
    "main",
    "resolve_settings",
    "run",
    "run_doctor",
]
