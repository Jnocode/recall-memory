"""Verifiable service lifecycle for the Recall authority (task 6.9, R9.8).

    recall-memory-mcp service install     register a per-user supervisor unit
    recall-memory-mcp service status      is it installed / registered / running
    recall-memory-mcp service uninstall   remove the unit we created

Why this module exists
----------------------

The authority must survive the operator closing their terminal, and it must
survive a crash.  Both are supervisor problems, not application problems, so
this module does not invent a daemon: it renders a **per-user** unit for the
supervisor the OS already ships and drives it with explicit argv.

Non-negotiable rules encoded here
---------------------------------

* **Per-user only.**  ``systemctl --user`` / ``LaunchAgents`` / a per-user
  Scheduled Task.  Never a system unit, never a LaunchDaemon, never
  ``/RU SYSTEM``.  Installing the authority as root would break the
  single-owner OS lock model and the owner-only file permissions.
* **Single owner.**  Every backend is configured so the supervisor can never
  start a second instance (``MultipleInstancesPolicy=IgnoreNew`` etc.), and
  :func:`probe_authority` reports the real OS lock rather than a PID file.
  ``serve`` itself takes the lock; the supervisor is a convenience, not the
  mutex.
* **No secrets in unit files.**  :data:`UNIT_ENV_ALLOWLIST` is a closed list.
  Anything else — and anything that merely *looks* like a credential — is
  refused at render time with :class:`SecretInUnitError`.  A unit file is
  world-readable on some systems and is often copied into bug reports.
* **We only remove what we wrote.**  Every rendered unit carries
  :data:`MANAGED_MARKER`.  ``uninstall`` refuses to touch a unit without it,
  and never touches the database, the config file or the operator audit.
* **Nothing is claimed that was not done.**  ``--dry-run`` writes nothing and
  runs nothing; the report says so.  A failed supervisor command rolls the
  unit file back and is reported as a failure, not as success.
* **Diagnostics are redacted.**  ``status`` never prints an absolute path.
  ``install`` does print paths: the local operator has to know which file is
  about to be created (same rule as ``init``).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

from . import __version__, provisioning, redaction

# ---------------------------------------------------------------------------
# identity / constants
# ---------------------------------------------------------------------------

SERVICE_NAME: Final[str] = "recall-memory-mcp"
SYSTEMD_UNIT_NAME: Final[str] = "recall-memory-mcp.service"
LAUNCHD_LABEL: Final[str] = "org.recall.memory-mcp"
WINDOWS_TASK_NAME: Final[str] = "recall-memory-mcp"

#: Present in every unit we render.  ``uninstall`` refuses anything without it.
MANAGED_MARKER: Final[str] = "recall-memory-mcp:managed-unit:v1"

SUPERVISOR_SYSTEMD_USER: Final[str] = "systemd-user"
SUPERVISOR_LAUNCHD: Final[str] = "launchd"
SUPERVISOR_WINDOWS_SCHTASKS: Final[str] = "windows-schtasks"

SUPPORTED_SUPERVISORS: Final[tuple[str, ...]] = (
    SUPERVISOR_SYSTEMD_USER,
    SUPERVISOR_LAUNCHD,
    SUPERVISOR_WINDOWS_SCHTASKS,
)

#: The only environment variables that may be baked into a unit file.
#: Closed list on purpose: an operator who needs anything else must put it in
#: the config file, which is owner-only, instead of in the unit.
UNIT_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "RECALL_MCP_CONFIG_DIR",
    "RECALL_MCP_DB_PATH",
    "RECALL_MCP_HOST",
    "RECALL_MCP_PORT",
    "RECALL_MCP_MODE",
)

#: Names/values that must never reach a unit file even if someone widens the
#: allowlist by accident.
_SECRET_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|apikey|private[_-]?key"
    r"|session[_-]?key|credential|authorization|bearer)"
)

_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ServiceError(RuntimeError):
    """Base class for every service-lifecycle failure."""


class UnsupportedSupervisorError(ServiceError):
    """No per-user supervisor we can drive on this platform."""


class ServiceNotInitialisedError(ServiceError):
    """Refuse to install a unit that could not possibly serve."""


class ExecutableNotFoundError(ServiceError):
    """The installed console script could not be located."""


class UnitRefusedError(ServiceError):
    """Destination refusal: symlink, existing unit without --force, ..."""


class ForeignUnitError(ServiceError):
    """A unit exists at the target path that we did not write."""


class SecretInUnitError(ServiceError):
    """Someone tried to bake a credential into a unit file."""


class ReadBackError(ServiceError):
    """The unit file did not read back byte-identical to what we rendered."""


class SupervisorCommandError(ServiceError):
    """The supervisor rejected a register/unregister command."""


class AuthorityCoreUnavailableError(ServiceError):
    """``recall`` (recall-sqlite) is not importable, so no OS lock exists."""


class ServiceAlreadyRunningError(ServiceError):
    """Another authority process already owns this database."""


# ---------------------------------------------------------------------------
# lazy core imports
# ---------------------------------------------------------------------------


def load_lock_module() -> Any:
    """Import ``recall.authority_lock`` lazily (same lock the server uses)."""

    try:
        from recall import authority_lock  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise AuthorityCoreUnavailableError(
            "the recall-sqlite core is not installed, so the single-owner "
            "authority lock is unavailable; reinstall recall-memory-mcp"
        ) from exc
    return authority_lock


# ---------------------------------------------------------------------------
# environment / executable resolution
# ---------------------------------------------------------------------------


def assert_unit_env(pairs: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Validate the environment that will be baked into a unit file."""

    cleaned: list[tuple[str, str]] = []
    for name in sorted(pairs):
        value = pairs[name]
        if name not in UNIT_ENV_ALLOWLIST:
            raise SecretInUnitError(
                f"{name} may not be written into a unit file; only "
                f"{', '.join(UNIT_ENV_ALLOWLIST)} are allowed. Put anything "
                "else in the owner-only config file."
            )
        if _SECRET_SHAPE.search(name) or _SECRET_SHAPE.search(str(value)):
            raise SecretInUnitError(
                f"{name} looks like a credential; unit files are not a secret store"
            )
        text = str(value)
        if _CONTROL_CHARS.search(text) or "\n" in text or "\r" in text:
            raise UnitRefusedError(
                f"{name} contains control characters and cannot be written to a unit file"
            )
        cleaned.append((name, text))
    return tuple(cleaned)


def unit_environment(settings: Any) -> tuple[tuple[str, str], ...]:
    """The minimal, validated environment a supervised ``serve`` needs."""

    return assert_unit_env(
        {
            "RECALL_MCP_CONFIG_DIR": str(Path(settings.config_path).parent),
            "RECALL_MCP_DB_PATH": str(settings.db_path),
            "RECALL_MCP_HOST": str(settings.host),
            "RECALL_MCP_PORT": str(settings.port),
            "RECALL_MCP_MODE": str(getattr(settings.mode, "value", settings.mode)),
        }
    )


def resolve_executable(
    *, explicit: str | None = None, platform: str | None = None
) -> Path:
    """Locate the installed ``recall-memory-mcp`` console script.

    A unit file must point at a real executable.  If we cannot find one we
    refuse rather than writing a unit that would fail at boot.
    """

    platform = sys.platform if platform is None else platform
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_file():
            raise ExecutableNotFoundError(
                "the executable given with --executable does not exist"
            )
        return candidate.resolve()

    name = f"{SERVICE_NAME}.exe" if platform.startswith("win") else SERVICE_NAME
    bindir = Path(sys.executable).resolve().parent
    candidate = bindir / name
    if candidate.is_file():
        return candidate
    found = shutil.which(SERVICE_NAME)
    if found:
        return Path(found).resolve()
    raise ExecutableNotFoundError(
        f"could not find the `{SERVICE_NAME}` console script next to the running "
        "interpreter or on PATH; install the package into this environment first"
    )


def detect_supervisor(
    *, platform: str | None = None, which: Callable[[str], str | None] = shutil.which
) -> str:
    """Pick the per-user supervisor for this platform, or fail closed."""

    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        if which("schtasks") is None:
            raise UnsupportedSupervisorError(
                "schtasks was not found; cannot register a per-user Scheduled Task"
            )
        return SUPERVISOR_WINDOWS_SCHTASKS
    if platform == "darwin":
        if which("launchctl") is None:
            raise UnsupportedSupervisorError(
                "launchctl was not found; cannot register a LaunchAgent"
            )
        return SUPERVISOR_LAUNCHD
    if platform.startswith("linux"):
        if which("systemctl") is None:
            raise UnsupportedSupervisorError(
                "systemctl was not found; this build only drives `systemctl --user`. "
                "Run `recall-memory-mcp serve` under your own supervisor instead."
            )
        return SUPERVISOR_SYSTEMD_USER
    raise UnsupportedSupervisorError(
        f"no supported per-user supervisor for platform {platform!r}; "
        "run `recall-memory-mcp serve` under your own supervisor instead"
    )


# ---------------------------------------------------------------------------
# unit paths
# ---------------------------------------------------------------------------


def unit_path_for(
    supervisor: str, settings: Any, env: Mapping[str, str] | None = None
) -> Path:
    """Where the unit definition for ``supervisor`` lives."""

    env = dict(os.environ) if env is None else dict(env)
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if supervisor == SUPERVISOR_SYSTEMD_USER:
        base = env.get("XDG_CONFIG_HOME")
        root = Path(base) if base else home / ".config"
        return root / "systemd" / "user" / SYSTEMD_UNIT_NAME
    if supervisor == SUPERVISOR_LAUNCHD:
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if supervisor == SUPERVISOR_WINDOWS_SCHTASKS:
        # The registration lives in the Task Scheduler; this XML is our
        # reproducible source of truth and sits beside the config we own.
        return Path(settings.config_path).parent / "service" / f"{SERVICE_NAME}.task.xml"
    raise UnsupportedSupervisorError(f"unknown supervisor {supervisor!r}")


def unit_encoding(supervisor: str) -> str:
    # schtasks /XML requires a Unicode task definition.
    return "utf-16" if supervisor == SUPERVISOR_WINDOWS_SCHTASKS else "utf-8"


# ---------------------------------------------------------------------------
# unit rendering
# ---------------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_systemd(executable: Path, env_pairs: Sequence[tuple[str, str]], workdir: Path) -> str:
    lines = [
        f"# {MANAGED_MARKER}",
        f"# rendered by {SERVICE_NAME} {__version__}; edit via `service install --force`",
        "# Single-owner authority: never run a second copy of this unit.",
        "",
        "[Unit]",
        "Description=Recall MCP authority (single-owner cross-client memory)",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={_quote_posix(executable)} serve",
        f"WorkingDirectory={workdir}",
    ]
    lines += [f'Environment="{name}={value}"' for name, value in env_pairs]
    lines += [
        "Restart=on-failure",
        "RestartSec=5",
        # A crash releases the OS lock; the restart re-takes it.  If the DB is
        # still owned (e.g. a manual `serve` is running) the restart exits 3
        # and systemd stops trying instead of fighting for the database.
        "SuccessExitStatus=3",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _quote_posix(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def _render_launchd(executable: Path, env_pairs: Sequence[tuple[str, str]], workdir: Path) -> str:
    env_xml = "".join(
        f"\n        <key>{_xml_escape(name)}</key>"
        f"\n        <string>{_xml_escape(value)}</string>"
        for name, value in env_pairs
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- {MANAGED_MARKER} -->
<!-- rendered by {SERVICE_NAME} {__version__}; per-user LaunchAgent only (never system-wide) -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>RecallManagedUnit</key>
    <string>{MANAGED_MARKER}</string>
    <key>ProgramArguments</key>
    <array>
      <string>{_xml_escape(str(executable))}</string>
      <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>{env_xml}
    </dict>
    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(workdir))}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
  </dict>
</plist>
"""


def _render_schtasks(executable: Path, env_pairs: Sequence[tuple[str, str]], workdir: Path) -> str:
    # schtasks has no environment block, so the settings travel as explicit
    # arguments.  Only allowlisted, non-secret values reach this point.
    args = " ".join(
        f"--{name.removeprefix('RECALL_MCP_').lower().replace('_', '-')} \"{value}\""
        for name, value in env_pairs
        if name in ("RECALL_MCP_CONFIG_DIR", "RECALL_MCP_DB_PATH", "RECALL_MCP_HOST")
    )
    port = dict(env_pairs).get("RECALL_MCP_PORT")
    if port:
        args = f"{args} --port {port}".strip()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<!-- {MANAGED_MARKER} -->
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{MANAGED_MARKER} - Recall MCP authority \
(single-owner cross-client memory), rendered by {SERVICE_NAME} {__version__}</Description>
    <URI>\\{WINDOWS_TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(str(executable))}</Command>
      <Arguments>serve {_xml_escape(args)}</Arguments>
      <WorkingDirectory>{_xml_escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def render_unit(
    supervisor: str,
    *,
    executable: Path,
    env_pairs: Sequence[tuple[str, str]],
    working_dir: Path,
) -> str:
    """Render the unit definition text for ``supervisor``."""

    # Re-validate: render_unit is a public entry point of its own.
    env_pairs = assert_unit_env(dict(env_pairs))
    if supervisor == SUPERVISOR_SYSTEMD_USER:
        return _render_systemd(executable, env_pairs, working_dir)
    if supervisor == SUPERVISOR_LAUNCHD:
        return _render_launchd(executable, env_pairs, working_dir)
    if supervisor == SUPERVISOR_WINDOWS_SCHTASKS:
        return _render_schtasks(executable, env_pairs, working_dir)
    raise UnsupportedSupervisorError(f"unknown supervisor {supervisor!r}")


# ---------------------------------------------------------------------------
# supervisor commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


CommandRunner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(argv: Sequence[str]) -> CommandResult:
    """Run a supervisor command with an explicit argv (never ``shell=True``)."""

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            # Supervisors localise their output (schtasks on a zh-TW Windows
            # emits CP950, systemctl emits UTF-8).  Decoding must never be
            # able to raise: a diagnostic command that crashes the CLI is a
            # worse bug than an unreadable byte.
            errors="replace",
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(tuple(argv), 127, "", "executable not found")
    except subprocess.TimeoutExpired:
        return CommandResult(tuple(argv), 124, "", "supervisor command timed out")
    except OSError as exc:  # pragma: no cover - platform quirk
        return CommandResult(tuple(argv), 126, "", type(exc).__name__)
    return CommandResult(
        tuple(argv), completed.returncode, completed.stdout or "", completed.stderr or ""
    )


def register_commands(supervisor: str, unit_path: Path) -> tuple[tuple[str, ...], ...]:
    if supervisor == SUPERVISOR_SYSTEMD_USER:
        return (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME),
        )
    if supervisor == SUPERVISOR_LAUNCHD:
        return (("launchctl", "load", "-w", str(unit_path)),)
    if supervisor == SUPERVISOR_WINDOWS_SCHTASKS:
        return (
            ("schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(unit_path), "/F"),
        )
    raise UnsupportedSupervisorError(f"unknown supervisor {supervisor!r}")


def unregister_commands(supervisor: str, unit_path: Path) -> tuple[tuple[str, ...], ...]:
    if supervisor == SUPERVISOR_SYSTEMD_USER:
        return (
            ("systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME),
            ("systemctl", "--user", "daemon-reload"),
        )
    if supervisor == SUPERVISOR_LAUNCHD:
        return (("launchctl", "unload", "-w", str(unit_path)),)
    if supervisor == SUPERVISOR_WINDOWS_SCHTASKS:
        return (("schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"),)
    raise UnsupportedSupervisorError(f"unknown supervisor {supervisor!r}")


def query_command(supervisor: str) -> tuple[str, ...]:
    if supervisor == SUPERVISOR_SYSTEMD_USER:
        return ("systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME)
    if supervisor == SUPERVISOR_LAUNCHD:
        return ("launchctl", "list", LAUNCHD_LABEL)
    if supervisor == SUPERVISOR_WINDOWS_SCHTASKS:
        return ("schtasks", "/Query", "/TN", WINDOWS_TASK_NAME)
    raise UnsupportedSupervisorError(f"unknown supervisor {supervisor!r}")


# ---------------------------------------------------------------------------
# unit file I/O
# ---------------------------------------------------------------------------


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise UnitRefusedError(
            "the unit path is a symbolic link; refusing to follow it"
        )


def read_unit(path: Path, supervisor: str) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return raw.decode(unit_encoding(supervisor))
    except UnicodeDecodeError:
        # A unit we did not write, in an encoding we do not use.
        try:
            return raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 # pragma: no cover
            return None


def is_managed_unit(path: Path, supervisor: str) -> bool:
    text = read_unit(path, supervisor)
    return bool(text) and MANAGED_MARKER in text


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return path.with_name(f"{path.name}.bak-{stamp}")


def _write_unit(path: Path, text: str, supervisor: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    path.write_bytes(text.encode(unit_encoding(supervisor)))
    provisioning.harden_file(path)


# ---------------------------------------------------------------------------
# authority probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityProbe:
    """What the OS lock says about *this* database, right now."""

    running: bool | None
    lock_supported: bool
    metadata_present: bool
    metadata_is_current: bool
    instance_id: str | None
    acquired_at: str | None
    db_identity: str | None
    reason: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "lock_supported": self.lock_supported,
            "metadata_present": self.metadata_present,
            "metadata_is_current": self.metadata_is_current,
            "instance_id": self.instance_id,
            "acquired_at": self.acquired_at,
            "db_identity": self.db_identity,
            "reason": self.reason,
        }


def probe_authority(db_path: str | Path) -> AuthorityProbe:
    """Ask the OS lock whether an authority process owns ``db_path``.

    Deliberately *not* a PID check: after a crash the PID in the sidecar is
    meaningless, and the whole point of the OS lock is that the kernel — not
    a heuristic — decides whether the previous owner is gone.
    """

    lock_mod = load_lock_module()
    lock = lock_mod.AuthorityDatabaseLock(db_path)
    metadata = lock.read_metadata()  # read *before* we perturb anything
    try:
        lock.acquire()
    except lock_mod.AuthorityLockHeldError:
        return AuthorityProbe(
            running=True,
            lock_supported=True,
            metadata_present=metadata is not None,
            metadata_is_current=metadata is not None,
            instance_id=getattr(metadata, "instance_id", None),
            acquired_at=getattr(metadata, "acquired_at", None),
            db_identity=getattr(metadata, "db_identity", None),
        )
    except lock_mod.AuthorityLockUnsupportedError as exc:
        return AuthorityProbe(
            running=None,
            lock_supported=False,
            metadata_present=metadata is not None,
            metadata_is_current=False,
            instance_id=None,
            acquired_at=None,
            db_identity=None,
            reason=str(exc),
        )
    else:
        lock.release()
        return AuthorityProbe(
            running=False,
            lock_supported=True,
            metadata_present=metadata is not None,
            # We just took and dropped the lock, so whoever wrote the sidecar
            # is definitively gone: the record is history, not state.
            metadata_is_current=False,
            instance_id=getattr(metadata, "instance_id", None),
            acquired_at=getattr(metadata, "acquired_at", None),
            db_identity=getattr(metadata, "db_identity", None),
        )


def acquire_single_owner(db_path: str | Path) -> Any:
    """Take the process-lifetime authority lock or fail closed.

    ``serve`` calls this before it opens a transport.  A crashed predecessor
    is not a problem: the kernel dropped its lock, so this call succeeds.
    """

    lock_mod = load_lock_module()
    lock = lock_mod.AuthorityDatabaseLock(db_path)
    try:
        lock.acquire()
    except lock_mod.AuthorityLockHeldError as exc:
        raise ServiceAlreadyRunningError(
            "another recall-memory-mcp authority process already owns this "
            "database; exactly one authority may run at a time. Stop the "
            "running service (`recall-memory-mcp service status`) first."
        ) from exc
    except lock_mod.AuthorityLockUnsupportedError as exc:
        raise ServiceAlreadyRunningError(
            f"refusing to serve: {exc}"
        ) from exc
    return lock


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceReport:
    action: str
    supervisor: str
    unit_path: Path | None
    dry_run: bool
    unit_written: bool = False
    unit_removed: bool = False
    backup_path: Path | None = None
    read_back_ok: bool | None = None
    registered: bool = False
    commands: tuple[tuple[str, ...], ...] = ()
    unit_text: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceStatus:
    supervisor: str | None
    supervisor_available: bool
    unit_present: bool
    unit_managed: bool | None
    query: CommandResult | None
    registered: bool | None
    authority: AuthorityProbe | None
    database_ready: bool
    database_schema_version: int
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0

    def redacted_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "supervisor": self.supervisor,
            "supervisor_available": self.supervisor_available,
            "unit_present": self.unit_present,
            "unit_managed": self.unit_managed,
            "registered": self.registered,
            "database": {
                "ready": self.database_ready,
                "schema_version": self.database_schema_version,
            },
            "authority": self.authority.payload() if self.authority else None,
            "detail": dict(self.detail),
        }
        if self.query is not None:
            payload["query"] = {
                "command": self.query.argv[0],
                "returncode": self.query.returncode,
            }
        return redaction.redact_mapping(payload)


# ---------------------------------------------------------------------------
# install / status / uninstall
# ---------------------------------------------------------------------------


def _resolved_supervisor(supervisor: str | None, platform: str | None) -> str:
    if supervisor is None:
        return detect_supervisor(platform=platform)
    if supervisor not in SUPPORTED_SUPERVISORS:
        raise UnsupportedSupervisorError(
            f"unknown supervisor {supervisor!r}; supported: "
            f"{', '.join(SUPPORTED_SUPERVISORS)}"
        )
    return supervisor


def install(
    settings: Any,
    *,
    supervisor: str | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    executable: str | None = None,
    unit_path: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> ServiceReport:
    """Render + install a per-user supervisor unit for the authority."""

    supervisor = _resolved_supervisor(supervisor, platform)
    runner = subprocess_runner if runner is None else runner

    status = provisioning.database_status(settings.db_path)
    if not status.exists or not status.ready:
        raise ServiceNotInitialisedError(
            "there is no ready authority database; run `recall-memory-mcp init` "
            "before installing a service. A unit that cannot serve is worse than "
            "no unit at all."
        )

    exe = resolve_executable(explicit=executable, platform=platform)
    env_pairs = unit_environment(settings)
    workdir = Path(settings.config_path).parent
    text = render_unit(
        supervisor, executable=exe, env_pairs=env_pairs, working_dir=workdir
    )

    target = Path(unit_path) if unit_path else unit_path_for(supervisor, settings, env)
    commands = register_commands(supervisor, target)

    if dry_run:
        return ServiceReport(
            action="install",
            supervisor=supervisor,
            unit_path=target,
            dry_run=True,
            unit_text=text,
            commands=commands,
            detail={
                "unit_exists": target.exists(),
                "would_write": True,
                "would_register": True,
                "executable": str(exe),
            },
        )

    _refuse_symlink(target)
    backup: Path | None = None
    existed = target.exists()
    if existed:
        if not force:
            raise UnitRefusedError(
                "a unit already exists at the target path; re-run with --force "
                "to back it up and replace it"
            )
        if not is_managed_unit(target, supervisor):
            raise ForeignUnitError(
                "the existing unit was not written by recall-memory-mcp; "
                "refusing to overwrite a file we do not own"
            )
        backup = _backup_path(target)
        shutil.copy2(target, backup)
        provisioning.harden_file(backup)

    previous = target.read_bytes() if existed else None
    try:
        _write_unit(target, text, supervisor)
        read_back = read_unit(target, supervisor)
        if read_back != text:
            raise ReadBackError(
                "the unit file did not read back identically after writing; "
                "nothing was registered"
            )
    except Exception:
        # Roll the filesystem back to exactly what we found.
        try:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
        except OSError:  # pragma: no cover - best effort rollback
            pass
        raise

    executed: list[tuple[str, ...]] = []
    for argv in commands:
        result = runner(argv)
        executed.append(tuple(argv))
        if not result.ok:
            # Undo the write: never leave a half-installed service behind.
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(previous)
            except OSError:  # pragma: no cover
                pass
            raise SupervisorCommandError(
                f"`{argv[0]}` failed with exit code {result.returncode}: "
                f"{redaction.redact_text(result.stderr.strip() or result.stdout.strip())}"
            )

    return ServiceReport(
        action="install",
        supervisor=supervisor,
        unit_path=target,
        dry_run=False,
        unit_written=True,
        backup_path=backup,
        read_back_ok=True,
        registered=True,
        commands=tuple(executed),
        detail={
            "replaced_existing": existed,
            "executable": str(exe),
            "owner_only": _owner_only(target),
        },
    )


def _owner_only(path: Path) -> bool:
    try:
        return provisioning.harden_file(path)
    except Exception:  # noqa: BLE001 # pragma: no cover
        return False


def status(
    settings: Any,
    *,
    supervisor: str | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    unit_path: str | Path | None = None,
) -> ServiceStatus:
    """Report installed / registered / running state without changing anything."""

    runner = subprocess_runner if runner is None else runner
    supervisor_available = True
    resolved: str | None
    detail: dict[str, Any] = {}
    try:
        resolved = _resolved_supervisor(supervisor, platform)
    except UnsupportedSupervisorError as exc:
        resolved = None
        supervisor_available = False
        detail["supervisor_reason"] = redaction.redact_text(str(exc))

    unit_present = False
    unit_managed: bool | None = None
    target: Path | None = None
    if resolved is not None:
        target = Path(unit_path) if unit_path else unit_path_for(resolved, settings, env)
        unit_present = target.exists() and not target.is_symlink()
        unit_managed = is_managed_unit(target, resolved) if unit_present else None

    query: CommandResult | None = None
    registered: bool | None = None
    if resolved is not None:
        query = runner(query_command(resolved))
        if query.returncode == 127:
            registered = None
            detail["query_reason"] = "supervisor executable not found"
        else:
            registered = query.ok

    db_status = provisioning.database_status(settings.db_path)
    authority: AuthorityProbe | None = None
    if db_status.exists:
        try:
            authority = probe_authority(settings.db_path)
        except AuthorityCoreUnavailableError as exc:  # pragma: no cover
            detail["authority_reason"] = redaction.redact_text(str(exc))

    return ServiceStatus(
        supervisor=resolved,
        supervisor_available=supervisor_available,
        unit_present=unit_present,
        unit_managed=unit_managed,
        query=query,
        registered=registered,
        authority=authority,
        database_ready=db_status.ready,
        database_schema_version=db_status.schema_version,
        detail=detail,
    )


def uninstall(
    settings: Any,
    *,
    supervisor: str | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    unit_path: str | Path | None = None,
    dry_run: bool = False,
) -> ServiceReport:
    """Remove the unit *we* wrote.  Never touches the database or config."""

    supervisor = _resolved_supervisor(supervisor, platform)
    runner = subprocess_runner if runner is None else runner
    target = Path(unit_path) if unit_path else unit_path_for(supervisor, settings, env)
    commands = unregister_commands(supervisor, target)

    exists = target.exists() and not target.is_symlink()
    managed = is_managed_unit(target, supervisor) if exists else None
    if exists and not managed:
        raise ForeignUnitError(
            "the unit at the target path was not written by recall-memory-mcp; "
            "refusing to remove a file we do not own"
        )

    if dry_run:
        return ServiceReport(
            action="uninstall",
            supervisor=supervisor,
            unit_path=target,
            dry_run=True,
            commands=commands,
            detail={"unit_exists": exists, "unit_managed": managed},
        )

    executed: list[tuple[str, ...]] = []
    failures: list[str] = []
    for argv in commands:
        result = runner(argv)
        executed.append(tuple(argv))
        if not result.ok:
            # Unregistering something that is not registered is not an error,
            # but we report it instead of silently claiming success.
            failures.append(
                f"{argv[0]} exit {result.returncode}: "
                f"{redaction.redact_text((result.stderr or result.stdout).strip())[:200]}"
            )

    removed = False
    if exists:
        target.unlink()
        removed = True

    return ServiceReport(
        action="uninstall",
        supervisor=supervisor,
        unit_path=target,
        dry_run=False,
        unit_removed=removed,
        commands=tuple(executed),
        detail={
            "unit_existed": exists,
            "supervisor_warnings": failures,
            "database_untouched": True,
        },
    )


__all__ = [
    "MANAGED_MARKER",
    "SUPPORTED_SUPERVISORS",
    "SUPERVISOR_LAUNCHD",
    "SUPERVISOR_SYSTEMD_USER",
    "SUPERVISOR_WINDOWS_SCHTASKS",
    "UNIT_ENV_ALLOWLIST",
    "AuthorityCoreUnavailableError",
    "AuthorityProbe",
    "CommandResult",
    "ExecutableNotFoundError",
    "ForeignUnitError",
    "ReadBackError",
    "SecretInUnitError",
    "ServiceAlreadyRunningError",
    "ServiceError",
    "ServiceNotInitialisedError",
    "ServiceReport",
    "ServiceStatus",
    "SupervisorCommandError",
    "UnitRefusedError",
    "UnsupportedSupervisorError",
    "acquire_single_owner",
    "assert_unit_env",
    "detect_supervisor",
    "install",
    "is_managed_unit",
    "probe_authority",
    "register_commands",
    "render_unit",
    "resolve_executable",
    "status",
    "subprocess_runner",
    "uninstall",
    "unit_environment",
    "unit_path_for",
    "unregister_commands",
    "query_command",
]
