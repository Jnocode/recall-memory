"""Service lifecycle tests (task 6.9, R9.8).

What this suite proves:

* ``service install`` renders a **per-user** unit for the platform supervisor,
  writes it only after a byte-identical read-back, and registers it with an
  exact argv — or rolls the file back and reports failure.
* A unit file is never a secret store: the environment allowlist is closed and
  credential-shaped names/values are refused at render time.
* ``service uninstall`` removes only units carrying our managed marker, and
  never touches the database.
* ``service status`` is redacted diagnostics: no absolute paths, and the
  "is it running" answer comes from the **OS authority lock**, not a PID file.
* ``serve`` takes the single-owner lock for its process lifetime, so a second
  authority fails closed — and a *hard-killed* authority (SIGKILL /
  TerminateProcess, no cleanup code) leaves a database that the next process
  can take over immediately, with its data intact.

Every test runs against a real migrated authority DB inside ``tmp_path`` with
a synthetic HOME/TEMP; the operator's real database is never touched, and no
real supervisor is ever driven (the command runner is injected).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from recall_memory_mcp import cli, provisioning, servicectl
from recall_memory_mcp.settings import ServerSettings

pytestmark = pytest.mark.usefixtures("isolated_env")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
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


@pytest.fixture
def settings(tmp_path: Path, isolated_env: dict[str, str]) -> ServerSettings:
    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(tmp_path / "authority")
    env["RECALL_MCP_DB_PATH"] = str(tmp_path / "authority" / "recall.db")
    resolved = ServerSettings.from_env(env)
    provisioning.initialize(resolved)
    return resolved


@pytest.fixture
def cli_env(settings: ServerSettings, isolated_env: dict[str, str]) -> dict[str, str]:
    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(Path(settings.config_path).parent)
    env["RECALL_MCP_DB_PATH"] = str(settings.db_path)
    return env


class FakeRunner:
    """Records supervisor argv; returns a scripted exit code per executable."""

    def __init__(self, returncodes: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.returncodes = returncodes or {}

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        code = self.returncodes.get(argv[0], 0)
        return servicectl.CommandResult(argv, code, "", "denied" if code else "")


def run_cli(argv, env, *, runner: Any = None):
    out, err = io.StringIO(), io.StringIO()
    parser = cli.build_parser()
    args = parser.parse_args(list(argv))
    if runner is not None:
        args.runner = runner
    code = args.handler(args, dict(env), out, err)
    return code, out.getvalue(), err.getvalue()


ALL_SUPERVISORS = list(servicectl.SUPPORTED_SUPERVISORS)


def unit_for(supervisor: str, settings: ServerSettings) -> str:
    return servicectl.render_unit(
        supervisor,
        executable=Path("/opt/venv/bin/recall-memory-mcp"),
        env_pairs=servicectl.unit_environment(settings),
        working_dir=Path(settings.config_path).parent,
    )


# ---------------------------------------------------------------------------
# supervisor detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", servicectl.SUPERVISOR_WINDOWS_SCHTASKS),
        ("darwin", servicectl.SUPERVISOR_LAUNCHD),
        ("linux", servicectl.SUPERVISOR_SYSTEMD_USER),
    ],
)
def test_detects_the_per_user_supervisor_for_each_platform(platform, expected) -> None:
    assert servicectl.detect_supervisor(platform=platform, which=lambda _n: "/usr/bin/x") == expected


def test_unknown_platform_fails_closed_instead_of_guessing() -> None:
    with pytest.raises(servicectl.UnsupportedSupervisorError) as excinfo:
        servicectl.detect_supervisor(platform="sunos5", which=lambda _n: "/usr/bin/x")
    assert "sunos5" in str(excinfo.value)


def test_missing_supervisor_binary_fails_closed() -> None:
    with pytest.raises(servicectl.UnsupportedSupervisorError) as excinfo:
        servicectl.detect_supervisor(platform="linux", which=lambda _n: None)
    assert "systemctl" in str(excinfo.value)


# ---------------------------------------------------------------------------
# unit environment: a unit file is not a secret store
# ---------------------------------------------------------------------------


def test_unit_env_allowlist_is_closed() -> None:
    with pytest.raises(servicectl.SecretInUnitError):
        servicectl.assert_unit_env({"RECALL_MCP_OAUTH_CLIENT_SECRET": "s3cret"})


@pytest.mark.parametrize(
    "name",
    [
        "RECALL_MCP_STATIC_API_KEY",
        "RECALL_MCP_TOKEN",
        "RECALL_MCP_OAUTH_CLIENT_SECRET",
        "AWS_SECRET_ACCESS_KEY",
        "RECALL_MCP_PASSWORD",
    ],
)
def test_credential_shaped_names_are_refused(name: str) -> None:
    with pytest.raises(servicectl.SecretInUnitError):
        servicectl.assert_unit_env({name: "value"})


def test_control_characters_cannot_be_injected_into_a_unit(tmp_path: Path) -> None:
    with pytest.raises(servicectl.UnitRefusedError):
        servicectl.assert_unit_env({"RECALL_MCP_HOST": "127.0.0.1\nExecStart=/bin/sh"})


def test_unit_environment_carries_only_the_allowlist(settings) -> None:
    names = [name for name, _ in servicectl.unit_environment(settings)]
    assert set(names) <= set(servicectl.UNIT_ENV_ALLOWLIST)
    assert "RECALL_MCP_DB_PATH" in names


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("supervisor", ALL_SUPERVISORS)
def test_every_unit_carries_the_managed_marker(supervisor, settings) -> None:
    assert servicectl.MANAGED_MARKER in unit_for(supervisor, settings)


@pytest.mark.parametrize("supervisor", ALL_SUPERVISORS)
def test_no_unit_contains_a_credential(supervisor, settings) -> None:
    text = unit_for(supervisor, settings).lower()
    # `InteractiveToken` is a Windows *logon type*, not a credential; it is the
    # only place a credential-shaped word may legitimately appear.
    text = text.replace("interactivetoken", "<logon-type>")
    for needle in ("secret", "token", "password", "passwd", "api_key", "apikey", "bearer"):
        assert needle not in text, f"{needle!r} leaked into the {supervisor} unit"


@pytest.mark.parametrize("supervisor", ALL_SUPERVISORS)
def test_units_reference_the_real_executable_and_the_serve_verb(supervisor, settings) -> None:
    text = unit_for(supervisor, settings)
    assert "recall-memory-mcp" in text
    assert "serve" in text


def test_systemd_unit_is_a_user_unit_with_a_restart_policy(settings) -> None:
    text = unit_for(servicectl.SUPERVISOR_SYSTEMD_USER, settings)
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text  # user target, not multi-user.target
    assert "multi-user.target" not in text
    assert "User=root" not in text
    # Exit 3 means "another authority owns the DB" — restarting would just
    # fight for the database, so it must not count as a failure.
    assert "SuccessExitStatus=3" in text


def test_launchd_unit_is_an_agent_not_a_daemon(settings) -> None:
    text = unit_for(servicectl.SUPERVISOR_LAUNCHD, settings)
    assert "<key>Label</key>" in text
    assert "KeepAlive" in text
    assert "LaunchDaemon" not in text
    assert "<key>UserName</key>" not in text  # would mean run-as-another-user


def test_schtasks_unit_is_least_privilege_and_single_instance(settings) -> None:
    text = unit_for(servicectl.SUPERVISOR_WINDOWS_SCHTASKS, settings)
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in text
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text
    assert "HighestAvailable" not in text
    assert "<LogonTrigger>" in text


def test_render_refuses_an_unknown_supervisor(settings) -> None:
    with pytest.raises(servicectl.UnsupportedSupervisorError):
        servicectl.render_unit(
            "systemd-system",
            executable=Path("/x/recall-memory-mcp"),
            env_pairs=servicectl.unit_environment(settings),
            working_dir=Path("/x"),
        )


def test_render_revalidates_the_environment_even_if_called_directly(settings) -> None:
    with pytest.raises(servicectl.SecretInUnitError):
        servicectl.render_unit(
            servicectl.SUPERVISOR_SYSTEMD_USER,
            executable=Path("/x/recall-memory-mcp"),
            env_pairs=(("RECALL_MCP_STATIC_API_KEY", "abc"),),
            working_dir=Path("/x"),
        )


# ---------------------------------------------------------------------------
# unit paths
# ---------------------------------------------------------------------------


def test_systemd_unit_path_is_the_user_unit_directory(settings, isolated_env) -> None:
    path = servicectl.unit_path_for(
        servicectl.SUPERVISOR_SYSTEMD_USER, settings, isolated_env
    )
    assert path.parts[-3:] == ("systemd", "user", "recall-memory-mcp.service")
    assert "/etc/systemd" not in path.as_posix()


def test_launchd_unit_path_is_launchagents(settings, isolated_env) -> None:
    path = servicectl.unit_path_for(servicectl.SUPERVISOR_LAUNCHD, settings, isolated_env)
    assert path.parent.name == "LaunchAgents"
    assert "LaunchDaemons" not in path.as_posix()


def test_schtasks_unit_path_sits_beside_the_config_we_own(settings, isolated_env) -> None:
    path = servicectl.unit_path_for(
        servicectl.SUPERVISOR_WINDOWS_SCHTASKS, settings, isolated_env
    )
    assert path.parent.parent == Path(settings.config_path).parent


# ---------------------------------------------------------------------------
# executable resolution
# ---------------------------------------------------------------------------


def test_explicit_executable_must_exist(tmp_path: Path) -> None:
    with pytest.raises(servicectl.ExecutableNotFoundError):
        servicectl.resolve_executable(explicit=str(tmp_path / "nope"))


def test_explicit_executable_is_accepted_when_present(tmp_path: Path) -> None:
    exe = tmp_path / "recall-memory-mcp"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    assert servicectl.resolve_executable(explicit=str(exe)) == exe.resolve()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "recall-memory-mcp"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    return exe


def install(settings, tmp_path, fake_exe, **kwargs):
    kwargs.setdefault("supervisor", servicectl.SUPERVISOR_SYSTEMD_USER)
    kwargs.setdefault("unit_path", tmp_path / "units" / "recall-memory-mcp.service")
    kwargs.setdefault("executable", str(fake_exe))
    return servicectl.install(settings, **kwargs)


def test_install_writes_the_unit_and_registers_it(settings, tmp_path, fake_exe) -> None:
    runner = FakeRunner()
    report = install(settings, tmp_path, fake_exe, runner=runner)

    assert report.unit_written is True
    assert report.read_back_ok is True
    assert report.registered is True
    assert report.unit_path.is_file()
    assert servicectl.MANAGED_MARKER in report.unit_path.read_text(encoding="utf-8")
    assert runner.calls == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "recall-memory-mcp.service"),
    ]


def test_install_refuses_without_an_initialised_authority(
    tmp_path: Path, isolated_env, fake_exe
) -> None:
    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(tmp_path / "empty")
    env["RECALL_MCP_DB_PATH"] = str(tmp_path / "empty" / "recall.db")
    bare = ServerSettings.from_env(env)

    with pytest.raises(servicectl.ServiceNotInitialisedError):
        install(bare, tmp_path, fake_exe, runner=FakeRunner())


def test_install_refuses_to_overwrite_an_existing_unit(settings, tmp_path, fake_exe) -> None:
    runner = FakeRunner()
    install(settings, tmp_path, fake_exe, runner=runner)
    with pytest.raises(servicectl.UnitRefusedError):
        install(settings, tmp_path, fake_exe, runner=runner)


def test_install_force_backs_up_the_previous_unit(settings, tmp_path, fake_exe) -> None:
    runner = FakeRunner()
    first = install(settings, tmp_path, fake_exe, runner=runner)
    original = first.unit_path.read_bytes()

    second = install(settings, tmp_path, fake_exe, runner=runner, force=True)
    assert second.backup_path is not None
    assert second.backup_path.read_bytes() == original
    assert second.detail["replaced_existing"] is True


def test_install_force_refuses_a_unit_we_did_not_write(settings, tmp_path, fake_exe) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    with pytest.raises(servicectl.ForeignUnitError):
        install(settings, tmp_path, fake_exe, runner=FakeRunner(), force=True)
    # untouched
    assert unit.read_text(encoding="utf-8") == "[Service]\nExecStart=/bin/true\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_install_refuses_a_symlinked_unit_path(settings, tmp_path, fake_exe) -> None:
    real = tmp_path / "elsewhere.service"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "units" / "recall-memory-mcp.service"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)

    with pytest.raises(servicectl.UnitRefusedError):
        install(settings, tmp_path, fake_exe, runner=FakeRunner())
    assert real.read_text(encoding="utf-8") == "x"


def test_failed_registration_rolls_the_unit_file_back(settings, tmp_path, fake_exe) -> None:
    runner = FakeRunner({"systemctl": 1})
    with pytest.raises(servicectl.SupervisorCommandError):
        install(settings, tmp_path, fake_exe, runner=runner)
    assert not (tmp_path / "units" / "recall-memory-mcp.service").exists()


def test_failed_registration_restores_the_previous_unit(settings, tmp_path, fake_exe) -> None:
    ok = FakeRunner()
    first = install(settings, tmp_path, fake_exe, runner=ok)
    original = first.unit_path.read_bytes()

    broken = FakeRunner({"systemctl": 1})
    with pytest.raises(servicectl.SupervisorCommandError):
        install(settings, tmp_path, fake_exe, runner=broken, force=True)
    assert first.unit_path.read_bytes() == original


def test_dry_run_writes_nothing_and_runs_nothing(settings, tmp_path, fake_exe) -> None:
    runner = FakeRunner()
    report = install(settings, tmp_path, fake_exe, runner=runner, dry_run=True)

    assert report.dry_run is True
    assert report.unit_written is False
    assert report.registered is False
    assert runner.calls == []
    assert not report.unit_path.exists()
    assert servicectl.MANAGED_MARKER in (report.unit_text or "")
    assert report.commands == (
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "recall-memory-mcp.service"),
    )


def test_installed_unit_reads_back_byte_identical(settings, tmp_path, fake_exe) -> None:
    report = install(settings, tmp_path, fake_exe, runner=FakeRunner())
    rendered = servicectl.render_unit(
        servicectl.SUPERVISOR_SYSTEMD_USER,
        executable=fake_exe.resolve(),
        env_pairs=servicectl.unit_environment(settings),
        working_dir=Path(settings.config_path).parent,
    )
    assert servicectl.read_unit(report.unit_path, servicectl.SUPERVISOR_SYSTEMD_USER) == rendered


def test_windows_unit_is_written_as_unicode_and_reads_back(settings, tmp_path, fake_exe) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.task.xml"
    report = servicectl.install(
        settings,
        supervisor=servicectl.SUPERVISOR_WINDOWS_SCHTASKS,
        unit_path=unit,
        executable=str(fake_exe),
        runner=FakeRunner(),
    )
    raw = unit.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 BOM: schtasks /XML needs it
    assert report.read_back_ok is True
    assert servicectl.is_managed_unit(unit, servicectl.SUPERVISOR_WINDOWS_SCHTASKS)


def test_install_registers_schtasks_with_an_exact_argv(settings, tmp_path, fake_exe) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.task.xml"
    runner = FakeRunner()
    servicectl.install(
        settings,
        supervisor=servicectl.SUPERVISOR_WINDOWS_SCHTASKS,
        unit_path=unit,
        executable=str(fake_exe),
        runner=runner,
    )
    assert runner.calls == [
        ("schtasks", "/Create", "/TN", "recall-memory-mcp", "/XML", str(unit), "/F")
    ]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_subprocess_runner_survives_non_utf8_supervisor_output() -> None:
    """Regression: schtasks on a zh-TW Windows emits CP950, not UTF-8.

    The clean-wheel smoke caught this: a `UnicodeDecodeError` was raised on a
    reader thread while `service status` ran a real `schtasks /Query`.  A
    read-only diagnostic must never be able to blow up on locale bytes.
    """

    result = servicectl.subprocess_runner(
        [
            sys.executable,
            "-c",
            r"import sys; sys.stdout.buffer.write(b'\xff\xfe caf\xe9 \x81\x40')",
        ]
    )
    assert result.returncode == 0
    assert isinstance(result.stdout, str)


def test_subprocess_runner_reports_a_missing_supervisor_as_127() -> None:
    result = servicectl.subprocess_runner(
        ["definitely-not-a-real-supervisor-binary-9f2a", "--version"]
    )
    assert result.returncode == 127
    assert result.ok is False


def test_status_survives_a_supervisor_that_emits_locale_bytes(settings, tmp_path) -> None:
    report = servicectl.status(
        settings,
        supervisor=servicectl.SUPERVISOR_WINDOWS_SCHTASKS,
        unit_path=tmp_path / "units" / "recall-memory-mcp.task.xml",
        runner=lambda argv: servicectl.subprocess_runner(
            [
                sys.executable,
                "-c",
                r"import sys; sys.stdout.buffer.write(b'\x81\x40\xe9'); raise SystemExit(1)",
            ]
        ),
    )
    assert report.registered is False
    json.dumps(report.redacted_payload())  # serialisable, no crash


def test_status_reports_a_missing_supervisor_binary_as_unknown(settings, tmp_path) -> None:
    report = servicectl.status(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=FakeRunner({"systemctl": 127}),
    )
    assert report.registered is None
    assert report.detail["query_reason"] == "supervisor executable not found"


def test_status_reports_absent_unit(settings, tmp_path) -> None:
    report = servicectl.status(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=FakeRunner({"systemctl": 3}),
    )
    assert report.unit_present is False
    assert report.unit_managed is None
    assert report.registered is False
    assert report.database_ready is True


def test_status_reports_a_managed_unit(settings, tmp_path, fake_exe) -> None:
    install(settings, tmp_path, fake_exe, runner=FakeRunner())
    report = servicectl.status(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=FakeRunner(),
    )
    assert report.unit_present is True
    assert report.unit_managed is True
    assert report.registered is True


def test_status_flags_a_foreign_unit(settings, tmp_path) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    report = servicectl.status(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=unit,
        runner=FakeRunner(),
    )
    assert report.unit_present is True
    assert report.unit_managed is False


def test_status_payload_leaks_no_path(settings, tmp_path, fake_exe) -> None:
    install(settings, tmp_path, fake_exe, runner=FakeRunner())
    report = servicectl.status(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=FakeRunner(),
    )
    blob = json.dumps(report.redacted_payload())
    assert str(tmp_path) not in blob
    assert str(settings.db_path) not in blob
    assert Path(settings.db_path).name not in blob


def test_status_uses_the_os_lock_not_a_pid_file(settings, tmp_path) -> None:
    from recall.authority_lock import AuthorityDatabaseLock

    holder = AuthorityDatabaseLock(settings.db_path)
    holder.acquire()
    try:
        busy = servicectl.probe_authority(settings.db_path)
    finally:
        holder.release()
    free = servicectl.probe_authority(settings.db_path)

    assert busy.running is True
    assert busy.metadata_is_current is True
    assert free.running is False
    # The sidecar still describes the previous owner, and status says so
    # instead of pretending the process is alive.
    assert free.metadata_is_current is False


def test_status_never_reports_running_from_stale_metadata(settings) -> None:
    from recall.authority_lock import AuthorityDatabaseLock, AuthorityLockMetadata

    lock = AuthorityDatabaseLock(settings.db_path)
    lock._write_metadata_for_test(
        AuthorityLockMetadata(
            instance_id="deadbeef",
            acquired_at="2000-01-01T00:00:00+00:00",
            db_identity="sha256:0000000000000000",
            pid=1,
        )
    )
    probe = servicectl.probe_authority(settings.db_path)
    assert probe.running is False
    assert probe.metadata_present is True
    assert probe.metadata_is_current is False


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_a_managed_unit(settings, tmp_path, fake_exe) -> None:
    install(settings, tmp_path, fake_exe, runner=FakeRunner())
    runner = FakeRunner()
    report = servicectl.uninstall(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=runner,
    )
    assert report.unit_removed is True
    assert not report.unit_path.exists()
    assert runner.calls == [
        ("systemctl", "--user", "disable", "--now", "recall-memory-mcp.service"),
        ("systemctl", "--user", "daemon-reload"),
    ]


def test_uninstall_refuses_a_foreign_unit(settings, tmp_path) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    runner = FakeRunner()

    with pytest.raises(servicectl.ForeignUnitError):
        servicectl.uninstall(
            settings,
            supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
            unit_path=unit,
            runner=runner,
        )
    assert unit.exists()
    assert runner.calls == []  # nothing was unregistered either


def test_uninstall_never_touches_the_database(settings, tmp_path, fake_exe) -> None:
    install(settings, tmp_path, fake_exe, runner=FakeRunner())
    before = Path(settings.db_path).read_bytes()
    servicectl.uninstall(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=FakeRunner(),
    )
    assert Path(settings.db_path).read_bytes() == before
    assert Path(settings.config_path).is_file()


def test_uninstall_dry_run_changes_nothing(settings, tmp_path, fake_exe) -> None:
    install(settings, tmp_path, fake_exe, runner=FakeRunner())
    runner = FakeRunner()
    report = servicectl.uninstall(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=runner,
        dry_run=True,
    )
    assert report.dry_run is True
    assert runner.calls == []
    assert report.unit_path.exists()


def test_uninstall_reports_supervisor_failures_instead_of_claiming_success(
    settings, tmp_path, fake_exe
) -> None:
    install(settings, tmp_path, fake_exe, runner=FakeRunner())
    report = servicectl.uninstall(
        settings,
        supervisor=servicectl.SUPERVISOR_SYSTEMD_USER,
        unit_path=tmp_path / "units" / "recall-memory-mcp.service",
        runner=FakeRunner({"systemctl": 5}),
    )
    assert report.detail["supervisor_warnings"]
    assert report.unit_removed is True


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_install_dry_run_prints_the_unit_and_the_commands(
    cli_env, tmp_path, fake_exe
) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    runner = FakeRunner()
    code, out, _err = run_cli(
        [
            "service",
            "install",
            "--supervisor",
            "systemd-user",
            "--unit-path",
            str(unit),
            "--executable",
            str(fake_exe),
            "--dry-run",
        ],
        cli_env,
        runner=runner,
    )
    assert code == cli.EXIT_OK
    assert "dry run" in out
    assert "would run: systemctl --user daemon-reload" in out
    assert servicectl.MANAGED_MARKER in out
    assert runner.calls == []
    assert not unit.exists()


def test_cli_install_then_status_then_uninstall(cli_env, tmp_path, fake_exe) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    common = ["--supervisor", "systemd-user", "--unit-path", str(unit)]

    code, out, err = run_cli(
        ["service", "install", *common, "--executable", str(fake_exe)],
        cli_env,
        runner=FakeRunner(),
    )
    assert code == cli.EXIT_OK, err
    assert "read-back OK" in out
    assert unit.is_file()

    code, out, err = run_cli(
        ["service", "status", *common, "--json"], cli_env, runner=FakeRunner()
    )
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert payload["unit_present"] is True
    assert payload["unit_managed"] is True
    assert payload["authority"]["running"] is False

    code, out, err = run_cli(
        ["service", "uninstall", *common], cli_env, runner=FakeRunner()
    )
    assert code == cli.EXIT_OK, err
    assert "unit removed: yes" in out
    assert "database    : untouched" in out
    assert not unit.exists()


def test_cli_install_refuses_before_init(tmp_path, isolated_env, fake_exe) -> None:
    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(tmp_path / "empty")
    env["RECALL_MCP_DB_PATH"] = str(tmp_path / "empty" / "recall.db")
    code, _out, err = run_cli(
        [
            "service",
            "install",
            "--supervisor",
            "systemd-user",
            "--unit-path",
            str(tmp_path / "u.service"),
            "--executable",
            str(fake_exe),
        ],
        env,
        runner=FakeRunner(),
    )
    assert code == cli.EXIT_REFUSED
    assert "init" in err


def test_cli_status_text_output_has_no_absolute_path(cli_env, tmp_path, fake_exe) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    run_cli(
        [
            "service",
            "install",
            "--supervisor",
            "systemd-user",
            "--unit-path",
            str(unit),
            "--executable",
            str(fake_exe),
        ],
        cli_env,
        runner=FakeRunner(),
    )
    _code, out, _err = run_cli(
        ["service", "status", "--supervisor", "systemd-user", "--unit-path", str(unit)],
        cli_env,
        runner=FakeRunner(),
    )
    assert str(tmp_path) not in out
    assert "recall.db" not in out


def test_cli_uninstall_refuses_a_foreign_unit(cli_env, tmp_path) -> None:
    unit = tmp_path / "units" / "recall-memory-mcp.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    code, _out, err = run_cli(
        ["service", "uninstall", "--supervisor", "systemd-user", "--unit-path", str(unit)],
        cli_env,
        runner=FakeRunner(),
    )
    assert code == cli.EXIT_REFUSED
    assert "not written by recall-memory-mcp" in err
    assert unit.exists()


def test_service_subcommands_are_a_closed_set() -> None:
    parser = cli.build_parser()
    for forbidden in ("start-as-root", "install-system", "db", "memory"):
        with pytest.raises(SystemExit):
            parser.parse_args(["service", forbidden])


# ---------------------------------------------------------------------------
# serve: single-owner lock, crash and restart  (the heart of task 6.9)
# ---------------------------------------------------------------------------


SRC_ROOT = str(Path(__file__).resolve().parents[1] / "src")
CORE_SRC = str(Path(__file__).resolve().parents[2] / "src")

_HOLDER_SCRIPT = """
import json, sys, time
sys.path.insert(0, {mcp_src!r})
sys.path.insert(0, {core_src!r})
from recall_memory_mcp import servicectl

lock = servicectl.acquire_single_owner({db!r})
print(json.dumps({{"status": "OWNED", "pid": lock.metadata.pid,
                   "instance_id": lock.metadata.instance_id}}), flush=True)
# Simulate a long-running authority: no atexit hook, no finally block that
# could tidy up when we are killed.
while True:
    time.sleep(0.2)
"""


def _start_authority(db_path: Path) -> tuple[subprocess.Popen, dict]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            _HOLDER_SCRIPT.format(mcp_src=SRC_ROOT, core_src=CORE_SRC, db=str(db_path)),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    if not line:
        proc.kill()
        raise AssertionError(f"authority stub produced no output: {proc.stderr.read()}")
    payload = json.loads(line)
    assert payload["status"] == "OWNED", payload
    return proc, payload


def test_second_serve_is_refused_while_an_authority_owns_the_database(
    settings, cli_env
) -> None:
    from recall.authority_lock import AuthorityDatabaseLock

    holder = AuthorityDatabaseLock(settings.db_path)
    holder.acquire()
    ran: list[Any] = []
    try:
        code, _out, err = run_cli(["serve"], cli_env, runner=lambda *a: ran.append(a))
    finally:
        holder.release()

    assert code == cli.EXIT_REFUSED
    assert "already owns this database" in err
    assert ran == []  # the transport was never started


def test_serve_releases_the_lock_when_it_returns(settings, cli_env) -> None:
    from recall.authority_lock import AuthorityDatabaseLock

    code, _out, err = run_cli(["serve"], cli_env, runner=lambda *a: None)
    assert code == cli.EXIT_OK, err

    after = AuthorityDatabaseLock(settings.db_path)
    after.acquire()  # would raise if `serve` leaked the lock
    after.release()


def test_serve_holds_the_lock_for_its_whole_lifetime(settings, cli_env) -> None:
    """While the runner is executing, nobody else may own the database."""

    observed: dict[str, Any] = {}

    def runner(_app, _settings, _env) -> None:
        observed["probe"] = servicectl.probe_authority(settings.db_path)

    code, _out, err = run_cli(["serve"], cli_env, runner=runner)
    assert code == cli.EXIT_OK, err
    assert observed["probe"].running is True


def test_hard_killed_authority_releases_ownership_and_restart_takes_over(
    settings, cli_env, tmp_path
) -> None:
    """The crash/restart proof task 6.9 asks for.

    A real second process owns the authority, gets ``kill -9`` /
    ``TerminateProcess`` (so *no* Python cleanup code runs), and we show that
    (1) while it lived nobody else could serve, (2) after the kill the OS
    released ownership with no cleanup step, (3) a restarted ``serve``
    acquires the lock, and (4) the database is still intact.
    """

    db = Path(settings.db_path)
    proc, owned = _start_authority(db)
    try:
        busy = servicectl.probe_authority(db)
        assert busy.running is True
        assert busy.instance_id == owned["instance_id"]

        # (1) a second authority must fail closed while the first is alive
        code, _out, err = run_cli(["serve"], cli_env, runner=lambda *a: None)
        assert code == cli.EXIT_REFUSED
        assert "already owns this database" in err
    finally:
        proc.kill()  # SIGKILL / TerminateProcess: no cleanup whatsoever
    proc.wait(timeout=60)

    # (2) the OS dropped the lock; no stale-lock cleanup command was needed.
    deadline = time.time() + 30
    while True:
        after = servicectl.probe_authority(db)
        if after.running is False:
            break
        assert time.time() < deadline, "ownership was never released after the kill"
        time.sleep(0.1)
    assert after.metadata_present is True  # the sidecar survived...
    assert after.metadata_is_current is False  # ...but is history, not state

    # (3) restart: `serve` takes ownership again and actually runs
    started: list[Any] = []

    def runner(_app, _settings, _env) -> None:
        started.append(servicectl.probe_authority(db).running)

    code, _out, err = run_cli(["serve"], cli_env, runner=runner)
    assert code == cli.EXIT_OK, err
    assert started == [True]

    # (4) the database survived the hard kill
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] >= 0
    conn.close()
    status = provisioning.database_status(db)
    assert status.ready is True


def test_status_after_a_crash_says_not_running(settings, cli_env) -> None:
    db = Path(settings.db_path)
    proc, _owned = _start_authority(db)
    try:
        report = servicectl.status(
            settings, supervisor=servicectl.SUPERVISOR_SYSTEMD_USER, runner=FakeRunner()
        )
        assert report.authority is not None
        assert report.authority.running is True
    finally:
        proc.kill()
    proc.wait(timeout=60)

    deadline = time.time() + 30
    while True:
        report = servicectl.status(
            settings, supervisor=servicectl.SUPERVISOR_SYSTEMD_USER, runner=FakeRunner()
        )
        if report.authority is not None and report.authority.running is False:
            break
        assert time.time() < deadline, "status still reports a dead authority as running"
        time.sleep(0.1)
    payload = report.redacted_payload()
    assert payload["authority"]["running"] is False
    assert payload["authority"]["metadata_is_current"] is False
