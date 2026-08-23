"""CLI tests (task 6.6) for `init` (6.2) and `doctor` (6.4).

Every test runs against an *isolated* HOME/APPDATA/TEMP mapping that is
handed to the CLI explicitly, so nothing here can read or write the real
operator's Recall database or configuration (R8.6, guardrail: the private
`~/.hermes/recall.db` is never touched).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from recall_memory_mcp import cli, provisioning

pytestmark = pytest.mark.usefixtures("isolated_env")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    """A completely synthetic environment: no real HOME, no real APPDATA."""

    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
    }


def run(argv, env, *, tmp_path: Path | None = None):
    """Invoke the CLI and return (exit_code, stdout, stderr)."""

    import io

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), env=dict(env), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "authority"


def init_env(tmp_path: Path, isolated_env: dict[str, str]) -> dict[str, str]:
    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(config_dir(tmp_path))
    return env


# ---------------------------------------------------------------------------
# 6.1 — console script + module wiring
# ---------------------------------------------------------------------------


def test_console_script_is_declared_in_pyproject() -> None:
    if sys.version_info < (3, 11):  # pragma: no cover - runtime is 3.11+
        pytest.skip("tomllib requires Python 3.11")
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["recall-memory-mcp"] == "recall_memory_mcp.cli:run"


def test_cli_exposes_run_and_main() -> None:
    assert callable(cli.main)
    assert callable(cli.run)


def test_no_arguments_is_a_usage_error(tmp_path: Path, isolated_env) -> None:
    code, _out, err = run([], isolated_env)
    assert code == cli.EXIT_USAGE
    assert "usage" in err.lower()


def test_unknown_command_is_a_usage_error(tmp_path: Path, isolated_env) -> None:
    code, _out, err = run(["frobnicate"], isolated_env)
    assert code == cli.EXIT_USAGE
    assert err.strip()


def test_version_flag_prints_single_version(tmp_path: Path, isolated_env) -> None:
    from recall_memory_mcp import __version__

    code, out, _err = run(["--version"], isolated_env)
    assert code == cli.EXIT_OK
    assert __version__ in out


# ---------------------------------------------------------------------------
# 6.2 — init
# ---------------------------------------------------------------------------


def test_init_creates_config_and_database(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    code, out, err = run(["init"], env)

    assert code == cli.EXIT_OK, err
    cfg = config_dir(tmp_path) / "config.toml"
    db = config_dir(tmp_path) / "authority.db"
    assert cfg.is_file()
    assert db.is_file()
    assert "schema version 1" in out


def test_init_shows_paths_before_creating_anything(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    code, out, _err = run(["init"], env)

    assert code == cli.EXIT_OK
    cfg = str(config_dir(tmp_path) / "config.toml")
    db = str(config_dir(tmp_path) / "authority.db")
    # Both target paths are disclosed to the local operator ...
    assert cfg in out
    assert db in out
    # ... strictly before anything is reported as created.
    assert out.index(cfg) < out.index("created")
    assert out.index(db) < out.index("created")


def test_init_database_passes_reopen_read_back(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    code, out, _err = run(["init"], env)
    assert code == cli.EXIT_OK
    assert "read-back" in out.lower()

    db = config_dir(tmp_path) / "authority.db"
    status = provisioning.database_status(db)
    assert status.exists is True
    assert status.schema_version == 1
    assert status.missing_tables == ()
    assert status.integrity_ok is True


def test_init_creates_every_required_table(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    db = config_dir(tmp_path) / "authority.db"
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert set(provisioning.REQUIRED_TABLES) <= names


def test_init_config_is_parseable_and_records_owner(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    parsed = provisioning.read_config(config_dir(tmp_path) / "config.toml")
    assert parsed["schema"] == provisioning.CONFIG_SCHEMA_VERSION
    assert parsed["owner_id"].startswith("owner-")
    assert parsed["server"]["host"] == "127.0.0.1"
    assert parsed["server"]["port"] == 8765
    assert parsed["database"]["path"] == str(config_dir(tmp_path) / "authority.db")


def test_init_config_never_contains_a_secret(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    env["RECALL_MCP_STATIC_API_KEY"] = "sk-do-not-persist-abcdefghijklmnop"
    env["RECALL_MCP_OAUTH_CLIENT_SECRET"] = "top-secret-client-value"
    assert run(["init"], env)[0] == cli.EXIT_OK

    text = (config_dir(tmp_path) / "config.toml").read_text(encoding="utf-8")
    assert "sk-do-not-persist" not in text
    assert "top-secret-client-value" not in text


def test_init_refuses_to_overwrite_an_existing_config(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    cfg = config_dir(tmp_path) / "config.toml"
    db = config_dir(tmp_path) / "authority.db"
    before_cfg = cfg.read_bytes()
    before_db = db.read_bytes()

    code, _out, err = run(["init"], env)
    assert code == cli.EXIT_REFUSED
    assert "exist" in err.lower()
    assert cfg.read_bytes() == before_cfg
    assert db.read_bytes() == before_db


def test_init_refuses_when_only_the_database_exists(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    target = config_dir(tmp_path)
    target.mkdir(parents=True)
    (target / "authority.db").write_bytes(b"not really a database")

    code, _out, err = run(["init"], env)
    assert code == cli.EXIT_REFUSED
    assert "exist" in err.lower()
    assert (target / "authority.db").read_bytes() == b"not really a database"
    assert not (target / "config.toml").exists()


def test_init_honours_explicit_flags_over_environment(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    other = tmp_path / "elsewhere"
    code, _out, err = run(["init", "--config-dir", str(other)], env)

    assert code == cli.EXIT_OK, err
    assert (other / "config.toml").is_file()
    assert (other / "authority.db").is_file()
    assert not config_dir(tmp_path).exists()


def test_init_honours_custom_db_path(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    db = tmp_path / "custom" / "my-authority.db"
    code, _out, err = run(["init", "--db-path", str(db)], env)

    assert code == cli.EXIT_OK, err
    assert db.is_file()
    assert provisioning.database_status(db).schema_version == 1


def test_init_never_writes_into_the_isolated_home(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    home = Path(isolated_env["HOME"])
    assert list(home.rglob("*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_init_hardens_file_permissions_on_posix(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    cfg = config_dir(tmp_path) / "config.toml"
    db = config_dir(tmp_path) / "authority.db"
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert db.stat().st_mode & 0o777 == 0o600


def test_harden_database_covers_every_existing_sidecar(tmp_path: Path, monkeypatch) -> None:
    """WAL/SHM hold memory content, so they must be hardened too (R8.6)."""

    db = tmp_path / "authority.db"
    db.write_bytes(b"")
    wal, shm = provisioning.sidecar_paths(db)
    wal.write_bytes(b"")
    shm.write_bytes(b"")

    seen: list[Path] = []

    def record(path: Path) -> bool:
        seen.append(Path(path))
        return True

    monkeypatch.setattr(provisioning, "harden_file", record)
    assert provisioning.harden_database(db) is True
    assert set(seen) == {db, wal, shm}


def test_harden_database_skips_absent_sidecars(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "authority.db"
    db.write_bytes(b"")
    seen: list[Path] = []
    monkeypatch.setattr(provisioning, "harden_file", lambda p: seen.append(Path(p)) or True)

    assert provisioning.harden_database(db) is True
    assert seen == [db]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_init_hardens_wal_sidecars_on_posix(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    db = config_dir(tmp_path) / "authority.db"
    for sidecar in provisioning.sidecar_paths(db):
        if sidecar.exists():
            assert sidecar.stat().st_mode & 0o777 == 0o600


def test_init_reports_owner_only_permission_state(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    report = provisioning.initialize(
        cli.resolve_settings(env), owner_id="owner-fixed", now="2026-08-04T00:00:00+00:00"
    )
    assert isinstance(report.config_owner_only, bool)
    assert isinstance(report.db_owner_only, bool)
    assert report.owner_id == "owner-fixed"
    assert report.schema_version == 1


def test_init_rolls_back_a_failed_database_creation(tmp_path: Path, isolated_env, monkeypatch) -> None:
    env = init_env(tmp_path, isolated_env)

    def boom(conn, **kwargs):  # noqa: ANN001
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(provisioning, "_run_migrations", boom)
    code, _out, err = run(["init"], env)

    assert code == cli.EXIT_FAILURE
    assert err.strip()
    assert not (config_dir(tmp_path) / "authority.db").exists()
    assert not (config_dir(tmp_path) / "config.toml").exists()


def test_init_error_output_is_redacted(tmp_path: Path, isolated_env, monkeypatch) -> None:
    env = init_env(tmp_path, isolated_env)

    def boom(conn, **kwargs):  # noqa: ANN001
        raise RuntimeError(f"failed writing {tmp_path / 'authority' / 'authority.db'}")

    monkeypatch.setattr(provisioning, "_run_migrations", boom)
    code, _out, err = run(["init"], env)

    assert code == cli.EXIT_FAILURE
    assert "authority.db" not in err
    assert "[REDACTED]" in err


# ---------------------------------------------------------------------------
# 6.4 — doctor
# ---------------------------------------------------------------------------


DOCTOR_CHECKS = ("package", "sdk", "config", "database", "embedding", "auth", "transport")


def test_doctor_runs_every_required_check(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    code, out, err = run(["doctor"], env)
    assert code == cli.EXIT_OK, err
    for name in DOCTOR_CHECKS:
        assert name in out


def test_doctor_json_is_valid_and_covers_every_check(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    code, out, _err = run(["doctor", "--json"], env)
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert {check["name"] for check in payload["checks"]} == set(DOCTOR_CHECKS)
    # An unauthenticated loopback install is a *warning*, never a failure:
    # doctor still exits 0 but the operator is told the port is unprotected.
    assert payload["status"] == "warn"
    assert all(check["status"] in {"ok", "warn"} for check in payload["checks"])
    assert {check["name"] for check in payload["checks"] if check["status"] == "warn"} == {"auth"}


def test_doctor_reports_sdk_and_package_versions(tmp_path: Path, isolated_env) -> None:
    from recall_memory_mcp import MCP_SDK_REQUIREMENT, __version__

    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    payload = json.loads(run(["doctor", "--json"], env)[1])
    checks = {check["name"]: check for check in payload["checks"]}

    assert checks["package"]["detail"]["version"] == __version__
    assert checks["sdk"]["detail"]["requirement"] == MCP_SDK_REQUIREMENT
    assert checks["sdk"]["detail"]["installed"].startswith("2.0")
    assert checks["sdk"]["status"] == "ok"


def test_doctor_reports_database_schema_and_integrity(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    payload = json.loads(run(["doctor", "--json"], env)[1])
    checks = {check["name"]: check for check in payload["checks"]}

    assert checks["database"]["status"] == "ok"
    assert checks["database"]["detail"]["schema_version"] == 1
    assert checks["database"]["detail"]["integrity_ok"] is True
    assert checks["database"]["detail"]["missing_tables"] == []


def test_doctor_output_never_leaks_paths(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    _code, out, err = run(["doctor"], env)
    _code2, jout, _jerr = run(["doctor", "--json"], env)

    for blob in (out, err, jout):
        assert str(config_dir(tmp_path)) not in blob
        assert "authority.db" not in blob
        assert "config.toml" not in blob


def test_doctor_output_never_leaks_secrets(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    env["RECALL_MCP_STATIC_API_KEY"] = "sk-doctor-must-not-print-abcdefgh"
    env["RECALL_MCP_OAUTH_CLIENT_SECRET"] = "doctor-client-secret-value"

    _code, out, _err = run(["doctor"], env)
    _code2, jout, _jerr = run(["doctor", "--json"], env)
    for blob in (out, jout):
        assert "sk-doctor-must-not-print" not in blob
        assert "doctor-client-secret-value" not in blob


def test_doctor_fails_closed_when_not_initialised(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    code, out, _err = run(["doctor"], env)

    assert code == cli.EXIT_FAILURE
    assert "init" in out
    assert str(config_dir(tmp_path)) not in out


def test_doctor_detects_a_corrupt_database(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    (config_dir(tmp_path) / "authority.db").write_bytes(b"corrupted" * 64)

    code, out, _err = run(["doctor", "--json"], env)
    assert code == cli.EXIT_FAILURE
    payload = json.loads(out)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["database"]["status"] == "fail"


def test_doctor_flags_a_database_missing_mcp_tables(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    db = config_dir(tmp_path) / "authority.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE mcp_idempotency")
        conn.commit()
    finally:
        conn.close()

    payload = json.loads(run(["doctor", "--json"], env)[1])
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["database"]["status"] == "fail"
    assert "mcp_idempotency" in checks["database"]["detail"]["missing_tables"]


def test_doctor_auth_check_warns_for_unauthenticated_local_mode(
    tmp_path: Path, isolated_env
) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    payload = json.loads(run(["doctor", "--json"], env)[1])
    checks = {check["name"]: check for check in payload["checks"]}

    assert checks["auth"]["detail"]["mode"] == "local"
    assert checks["auth"]["detail"]["require_auth"] is False
    assert checks["auth"]["detail"]["oauth_issuer_configured"] is False
    assert checks["auth"]["status"] == "warn"


def test_doctor_auth_check_passes_for_remote_mode(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    env.update(
        {
            "RECALL_MCP_MODE": "remote",
            "RECALL_MCP_PUBLIC_URL": "https://memory.example.test",
            "RECALL_MCP_OAUTH_ISSUER": "https://auth.example.test",
            "RECALL_MCP_ALLOWED_HOSTS": "memory.example.test",
        }
    )
    payload = json.loads(run(["doctor", "--json"], env)[1])
    checks = {check["name"]: check for check in payload["checks"]}

    assert checks["auth"]["status"] == "ok"
    assert checks["auth"]["detail"]["require_auth"] is True
    assert checks["auth"]["detail"]["oauth_issuer_configured"] is True
    assert checks["auth"]["detail"]["public_url_is_https"] is True


def test_doctor_transport_check_reports_loopback_binding(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    payload = json.loads(run(["doctor", "--json"], env)[1])
    checks = {check["name"]: check for check in payload["checks"]}

    assert checks["transport"]["detail"]["loopback_only"] is True
    assert checks["transport"]["detail"]["wildcard_allowlist"] is False
    assert checks["transport"]["detail"]["port"] == 8765
    assert checks["transport"]["detail"]["port_in_use"] in (True, False)


def test_doctor_reports_invalid_settings_without_traceback(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    env["RECALL_MCP_MODE"] = "not-a-mode"

    code, out, err = run(["doctor"], env)
    assert code == cli.EXIT_FAILURE
    assert "Traceback" not in err
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 6.3 — serve command tests
#
# NOTE (task 6.9): these two tests used to be nested *inside*
# `test_doctor_reports_invalid_settings_without_traceback`, so pytest never
# collected them and `serve` was in effect untested.  They are module-level
# functions now; `test_parser_registers_the_serve_tests` below pins that down
# so the same accident cannot come back unnoticed.
# ---------------------------------------------------------------------------


def test_serve_refuses_when_not_initialised(tmp_path: Path, isolated_env) -> None:
    env = init_env(tmp_path, isolated_env)
    code, _out, err = run(["serve"], env)
    assert code == cli.EXIT_REFUSED
    assert "no ready authority database" in err


def test_serve_runs_injectable_runner(tmp_path: Path, isolated_env) -> None:
    import io

    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK

    recorded: list[tuple[Any, Any, dict[str, str]]] = []

    def fake_runner(app: Any, settings: Any, merged_env: dict[str, str]) -> None:
        recorded.append((app, settings, merged_env))

    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--port", "19889"])
    args.runner = fake_runner
    out, err = io.StringIO(), io.StringIO()
    code = args.handler(args, env, out, err)

    assert code == cli.EXIT_OK, err.getvalue()
    assert len(recorded) == 1
    app, settings, _ = recorded[0]
    assert settings.port == 19889
    assert app is not None
    assert "starting recall-memory-mcp server" in out.getvalue()


def test_serve_passes_explicit_memory_scopes_to_authority(
    tmp_path: Path, isolated_env, monkeypatch
) -> None:
    import io

    env = init_env(tmp_path, isolated_env)
    assert run(["init"], env)[0] == cli.EXIT_OK
    env["RECALL_MCP_MEMORY_SCOPES"] = "global,project:recall-ide-e2e"

    captured: dict[str, Any] = {}
    sentinel_app = object()

    def fake_build_app(settings: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel_app

    recorded: list[Any] = []

    def fake_runner(app: Any, settings: Any, merged_env: dict[str, str]) -> None:
        recorded.append(app)

    monkeypatch.setattr(cli, "build_app", fake_build_app)
    parser = cli.build_parser()
    args = parser.parse_args(["serve"])
    args.runner = fake_runner
    out, err = io.StringIO(), io.StringIO()

    code = args.handler(args, env, out, err)

    assert code == cli.EXIT_OK, err.getvalue()
    assert recorded == [sentinel_app]
    assert captured["memory_scopes"] == ("global", "project:recall-ide-e2e")


def test_every_test_in_this_module_is_top_level() -> None:
    """Guard against the nesting accident that hid the `serve` tests.

    A test defined inside another test is never collected, so it silently
    proves nothing.  `__qualname__` differs from `__name__` exactly when a
    function is nested, which is the cheapest reliable detector.
    """

    import types

    module = sys.modules[__name__]
    nested = [
        name
        for name, obj in vars(module).items()
        if name.startswith("test_")
        and isinstance(obj, types.FunctionType)
        and obj.__qualname__ != name
    ]
    assert nested == []
    # And the two tests this guard was written for really are collectable.
    assert callable(getattr(module, "test_serve_refuses_when_not_initialised"))
    assert callable(getattr(module, "test_serve_runs_injectable_runner"))
