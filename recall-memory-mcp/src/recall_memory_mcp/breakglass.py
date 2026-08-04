"""Offline break-glass database operations (task 6.8, allowlist for 6.10c).

This module is the **only** place in the distribution that is allowed to
touch the authority SQLite file without going through the online MCP
authority.  Design.md §9.1 states the boundary precisely:

    Offline direct-DB only exists in the explicit ``db backup``,
    ``db restore --whole-database`` and ``db migrate`` break-glass
    subcommands; it is a trusted local OS operator boundary and it
    **does not provide and does not claim any MCP owner ACL**.

Everything here therefore obeys five hard rules:

1. **Allowlist, not denylist.**  :data:`OFFLINE_ALLOWED_OPERATIONS` is a
   closed set of three whole-database operations.  Every row-level or
   grant-level operation name is rejected by
   :func:`assert_offline_allowed` with a pointer to the online
   owner-scoped admin CLI.  A future refactor that adds a new offline verb
   fails the test-suite until it is consciously added to the allowlist.
2. **Never a plain file copy of a live database.**  Snapshots always go
   through the SQLite online backup API (``sqlite3.Connection.backup``,
   wrapped by ``recall.migrations.sqlite_backup``) so a WAL-mode database
   is captured transactionally.
3. **Proof that the service is stopped.**  Every operation acquires the
   same OS-backed :class:`~recall.authority_lock.AuthorityDatabaseLock`
   the server uses.  If a server owns the database the command fails
   closed instead of racing it.
4. **Explicit, non-scriptable confirmation.**  The required phrase embeds
   the redacted identity of *this* database, so ``--confirm yes`` in a
   script can never fire against the wrong file.
5. **Content-free operator audit.**  Every attempt (including refusals) is
   appended to a local JSONL audit log that holds no memory content, no
   filesystem paths, and no secrets — only the operation, the outcome, the
   redacted database identity, and structural counters.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from . import provisioning

__all__ = [
    "AuthorityBusyError",
    "AuthorityCoreUnavailableError",
    "BackupVerificationError",
    "BreakGlassError",
    "BreakGlassReport",
    "ConfirmationRequiredError",
    "DestinationRefusedError",
    "OFFLINE_ALLOWED_OPERATIONS",
    "OFFLINE_DENIED_OPERATIONS",
    "OperatorAuditLog",
    "OfflineOperationDeniedError",
    "SourceRefusedError",
    "assert_offline_allowed",
    "audit_path_for",
    "backup_database",
    "confirmation_phrase",
    "db_identity",
    "migrate_database",
    "restore_whole_database",
    "verify_backup",
    "warning_text",
]


# ---------------------------------------------------------------------------
# allowlist (tasks 6.8 / 6.10c)
# ---------------------------------------------------------------------------

#: The complete set of operations that may ever run against the authority
#: file offline.  All three are *whole-database* operations.
OFFLINE_ALLOWED_OPERATIONS: Final[tuple[str, ...]] = ("backup", "restore", "migrate")

_ONLINE_ADMIN_HINT: Final[str] = (
    "offline break-glass has no owner ACL; use the online owner-scoped admin CLI"
)
_NO_SUCH_PATH_HINT: Final[str] = (
    "offline break-glass has no owner ACL and never edits authorisation state"
)

#: Operation names that are explicitly refused offline, with the reason the
#: operator sees.  Task 6.10c pins every one of these down.
OFFLINE_DENIED_OPERATIONS: Final[Mapping[str, str]] = {
    "memory-list": _ONLINE_ADMIN_HINT,
    "memory-search": _ONLINE_ADMIN_HINT,
    "memory-get": _ONLINE_ADMIN_HINT,
    "memory-export": _ONLINE_ADMIN_HINT,
    "memory-restore": _ONLINE_ADMIN_HINT,
    "memory-purge": _ONLINE_ADMIN_HINT,
    "memory-add": _ONLINE_ADMIN_HINT,
    "memory-replace": _ONLINE_ADMIN_HINT,
    "memory-remove": _ONLINE_ADMIN_HINT,
    "row-restore": _ONLINE_ADMIN_HINT,
    "grant-list": _NO_SUCH_PATH_HINT,
    "grant-edit": _NO_SUCH_PATH_HINT,
    "grant-create": _NO_SUCH_PATH_HINT,
    "grant-revoke": _NO_SUCH_PATH_HINT,
    "owner-edit": _NO_SUCH_PATH_HINT,
    "sql": "arbitrary SQL against the authority database is never offered",
}


class BreakGlassError(RuntimeError):
    """Base class for every offline break-glass refusal or failure."""


class OfflineOperationDeniedError(BreakGlassError):
    """The requested operation is not on the offline allowlist."""

    def __init__(self, operation: str, reason: str) -> None:
        super().__init__(
            f"offline break-glass refuses {operation!r}: {reason}. "
            f"allowed offline operations: {', '.join(OFFLINE_ALLOWED_OPERATIONS)}"
        )
        self.operation = operation
        self.reason = reason


class ConfirmationRequiredError(BreakGlassError):
    """The operator did not supply the exact confirmation phrase."""


class AuthorityBusyError(BreakGlassError):
    """A server still owns the authority database; stop it first."""

    def __init__(self) -> None:
        super().__init__(
            "the authority database is still owned by a running process; "
            "stop the recall-memory-mcp service before running break-glass commands"
        )


class DestinationRefusedError(BreakGlassError):
    """The destination path is unsafe (symlink, existing file, directory)."""


class SourceRefusedError(BreakGlassError):
    """The source path is unsafe or is not a usable authority database."""


class BackupVerificationError(BreakGlassError):
    """Integrity or schema-version verification of a snapshot failed."""


class AuthorityCoreUnavailableError(BreakGlassError):
    """The canonical ``recall-sqlite`` core is not importable."""


def assert_offline_allowed(operation: str) -> str:
    """Return ``operation`` if it is on the offline allowlist, else raise.

    The check is a closed allowlist: an unknown verb is refused just as
    firmly as an explicitly denied one, so nothing can slip through by
    simply not being listed in :data:`OFFLINE_DENIED_OPERATIONS`.
    """

    normalised = (operation or "").strip().lower()
    if normalised in OFFLINE_ALLOWED_OPERATIONS:
        return normalised
    reason = OFFLINE_DENIED_OPERATIONS.get(
        normalised, "it is not a whole-database break-glass operation"
    )
    raise OfflineOperationDeniedError(operation, reason)


# ---------------------------------------------------------------------------
# canonical core access
# ---------------------------------------------------------------------------


def _load_migrations() -> Any:
    try:
        from recall import migrations  # noqa: PLC0415 - deliberately lazy
    except Exception as exc:  # noqa: BLE001
        raise AuthorityCoreUnavailableError(
            "the canonical recall-sqlite core is not installed"
        ) from exc
    return migrations


def _load_lock_module() -> Any:
    try:
        from recall import authority_lock  # noqa: PLC0415 - deliberately lazy
    except Exception as exc:  # noqa: BLE001
        raise AuthorityCoreUnavailableError(
            "the canonical recall-sqlite core is not installed"
        ) from exc
    return authority_lock


def db_identity(db_path: str | Path) -> str:
    """Redacted, stable identity of an authority file (``sha256:<16 hex>``)."""

    return _load_lock_module().db_identity_for(db_path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# confirmation (task 6.8: "not accidentally scriptable")
# ---------------------------------------------------------------------------


def confirmation_phrase(operation: str, db_path: str | Path) -> str:
    """Exact phrase the operator must type for ``operation`` on ``db_path``.

    The phrase binds the verb *and* the redacted identity of this specific
    database, so a copy-pasted confirmation from a runbook cannot fire
    against a different authority file.
    """

    verb = assert_offline_allowed(operation)
    return f"BREAK-GLASS {verb.upper()} {db_identity(db_path)}"


def warning_text(operation: str, db_path: str | Path) -> str:
    """Whole-database impact warning shown before any destructive action."""

    verb = assert_offline_allowed(operation)
    impact = {
        "backup": (
            "reads the ENTIRE authority database — every owner, every grant and "
            "every memory — into a new snapshot file. The snapshot is as sensitive "
            "as the database itself: store it encrypted and inside your backup "
            "retention policy."
        ),
        "restore": (
            "REPLACES the ENTIRE authority database. Every memory, grant and audit "
            "row written after the snapshot was taken is lost. There is no "
            "row-level restore here."
        ),
        "migrate": (
            "rewrites the schema of the ENTIRE authority database in place. A "
            "pre-migration snapshot is taken first and is your only way back."
        ),
    }[verb]
    return (
        f"BREAK-GLASS {verb.upper()} — offline direct-database operation.\n"
        "This is a trusted local OS operator boundary. It does NOT enforce MCP "
        "owner ACLs and it cannot be scoped to a single memory.\n"
        f"Impact: it {impact}\n"
        f"database identity: {db_identity(db_path)}\n"
        f"To proceed, re-run with:  --confirm '{confirmation_phrase(verb, db_path)}'"
    )


def _require_confirmation(operation: str, db_path: Path, confirm: str | None) -> None:
    expected = confirmation_phrase(operation, db_path)
    if confirm is None or confirm.strip() != expected:
        raise ConfirmationRequiredError(
            "the exact confirmation phrase is required for this break-glass "
            f"operation; expected: {expected}"
        )


# ---------------------------------------------------------------------------
# content-free operator audit
# ---------------------------------------------------------------------------

_AUDIT_SUFFIX: Final[str] = ".operator-audit.jsonl"

# Anything that looks like a filesystem path or a URI-with-host is refused
# outright: the audit trail must stay content-free and path-free.
_PATH_LIKE = re.compile(r"(^[A-Za-z]:[\\/])|([\\/])|(~)|(\.\.)")
_ALLOWED_STRING_KEYS_WITH_COLON = frozenset({"db_identity", "source_identity", "target_identity"})


class AuditContentError(BreakGlassError):
    """A caller tried to write content or a path into the operator audit."""


def audit_path_for(db_path: str | Path) -> Path:
    """Sidecar operator-audit log path for ``db_path``."""

    path = Path(db_path).expanduser()
    return path.with_name(path.name + _AUDIT_SUFFIX)


def _assert_content_free(value: Any, *, key: str = "") -> None:
    if isinstance(value, Mapping):
        for sub_key, sub_value in value.items():
            _assert_content_free(sub_value, key=str(sub_key))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_content_free(item, key=key)
        return
    if isinstance(value, str):
        if key in _ALLOWED_STRING_KEYS_WITH_COLON and value.startswith("sha256:"):
            return
        if _PATH_LIKE.search(value):
            raise AuditContentError(
                f"operator audit field {key!r} looks like a filesystem path; "
                "the break-glass audit trail is content-free and path-free"
            )


@dataclass(frozen=True)
class AuditEntry:
    at: str
    operation: str
    outcome: str
    db_identity: str
    event_id: str
    detail: dict[str, Any] = field(default_factory=dict)


class OperatorAuditLog:
    """Append-only, content-free JSONL audit of local break-glass activity."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        operation: str,
        outcome: str,
        db_identity: str,
        detail: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        payload = dict(detail or {})
        _assert_content_free(payload)
        _assert_content_free(db_identity, key="db_identity")
        entry = AuditEntry(
            at=_utc_now(),
            operation=operation,
            outcome=outcome,
            db_identity=db_identity,
            event_id=uuid.uuid4().hex,
            detail=payload,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
        provisioning.harden_file(self.path)
        return entry

    def entries(self) -> tuple[AuditEntry, ...]:
        """Read the audit trail back (task 6.10c requires a read-back)."""

        if not self.path.is_file():
            return ()
        rows: list[AuditEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            rows.append(
                AuditEntry(
                    at=str(payload["at"]),
                    operation=str(payload["operation"]),
                    outcome=str(payload["outcome"]),
                    db_identity=str(payload["db_identity"]),
                    event_id=str(payload["event_id"]),
                    detail=dict(payload.get("detail") or {}),
                )
            )
        return tuple(rows)


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------


def _refuse_symlink(path: Path, *, error: type[BreakGlassError], label: str) -> None:
    if path.is_symlink():
        raise error(f"{label} is a symbolic link; break-glass never follows symlinks")
    parent = path.parent
    if parent.exists() and parent.is_symlink():
        raise error(f"{label} directory is a symbolic link; break-glass never follows symlinks")


def _check_new_destination(destination: Path) -> Path:
    resolved = destination.expanduser()
    _refuse_symlink(resolved, error=DestinationRefusedError, label="destination")
    if resolved.is_dir():
        raise DestinationRefusedError("destination is a directory; give the snapshot a file name")
    if resolved.exists():
        raise DestinationRefusedError(
            "destination already exists; break-glass never overwrites an existing file"
        )
    return resolved


def _check_existing_source(source: Path) -> Path:
    resolved = source.expanduser()
    _refuse_symlink(resolved, error=SourceRefusedError, label="source")
    if not resolved.is_file():
        raise SourceRefusedError("source snapshot does not exist")
    return resolved


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupVerification:
    integrity_ok: bool
    schema_version: int
    missing_tables: tuple[str, ...]
    memory_count: int
    ready: bool


def verify_backup(path: str | Path) -> BackupVerification:
    """Integrity + schema-version + required-table verification of a snapshot."""

    status = provisioning.database_status(path)
    memory_count = 0
    if status.exists and status.integrity_ok and "memories" not in status.missing_tables:
        try:
            conn = sqlite3.connect(
                f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10.0
            )
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            except sqlite3.DatabaseError:
                memory_count = 0
            finally:
                conn.close()
    return BackupVerification(
        integrity_ok=status.integrity_ok,
        schema_version=status.schema_version,
        missing_tables=tuple(status.missing_tables),
        memory_count=memory_count,
        ready=status.ready,
    )


def _supported_schema_version() -> int:
    return int(_load_migrations().LATEST_SCHEMA_VERSION)


def _overwrite_via_backup_api(source: Path, target: Path) -> None:
    """Replace ``target``'s entire content with ``source``'s, transactionally.

    This is ``sqlite3.Connection.backup`` — the SQLite online backup API —
    pointed at an existing destination database.  It is deliberately *not*
    a filesystem copy: a live WAL-mode database cannot be replaced safely
    by copying bytes over it.
    """

    source_conn = sqlite3.connect(
        f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0
    )
    target_conn = sqlite3.connect(target, timeout=30.0)
    try:
        target_conn.execute("PRAGMA busy_timeout=30000")
        source_conn.backup(target_conn)
        target_conn.commit()
    finally:
        target_conn.close()
        source_conn.close()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakGlassReport:
    operation: str
    db_identity: str
    outcome: str
    detail: dict[str, Any]
    audit_event_id: str
    #: Real filesystem paths for the *local operator's* terminal only.  They
    #: are deliberately absent from the audit log and from every error
    #: message that could reach a client.
    produced_path: Path | None = None
    safety_backup_path: Path | None = None


# ---------------------------------------------------------------------------
# the three allowlisted operations
# ---------------------------------------------------------------------------


def _open_lock(db_path: Path, lock_factory: Callable[[Path], Any] | None) -> Any:
    if lock_factory is not None:
        return lock_factory(db_path)
    return _load_lock_module().AuthorityDatabaseLock(db_path)


class _ExclusiveAuthority:
    """Context manager proving the service is stopped, then holding the lock."""

    def __init__(self, db_path: Path, lock_factory: Callable[[Path], Any] | None) -> None:
        self._lock = _open_lock(db_path, lock_factory)

    def __enter__(self) -> Any:
        lock_module = _load_lock_module()
        try:
            self._lock.acquire()
        except lock_module.AuthorityLockHeldError as exc:
            raise AuthorityBusyError() from exc
        except lock_module.AuthorityLockUnsupportedError as exc:
            raise BreakGlassError(str(exc)) from exc
        return self._lock

    def __exit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


def _run_guarded(
    operation: str,
    db_path: str | Path,
    *,
    confirm: str | None,
    audit_log: OperatorAuditLog | None,
    lock_factory: Callable[[Path], Any] | None,
    body: Callable[[], tuple[dict[str, Any], Path | None, Path | None]],
) -> BreakGlassReport:
    """Shared allowlist / confirmation / lock / audit envelope."""

    verb = assert_offline_allowed(operation)
    path = Path(db_path).expanduser()
    identity = db_identity(path)
    log = audit_log if audit_log is not None else OperatorAuditLog(audit_path_for(path))

    try:
        _require_confirmation(verb, path, confirm)
    except ConfirmationRequiredError:
        log.append(
            operation=verb,
            outcome="refused",
            db_identity=identity,
            detail={"reason": "confirmation_phrase_missing_or_wrong"},
        )
        raise

    # Preflight *before* the lock: taking an authority lock would otherwise
    # create a sidecar file next to a database directory that does not exist.
    if not path.parent.is_dir():
        log.append(
            operation=verb,
            outcome="refused",
            db_identity=identity,
            detail={"reason": "authority_location_missing"},
        )
        raise SourceRefusedError(
            "there is no authority database at the configured location; run `init` first"
        )

    try:
        with _ExclusiveAuthority(path, lock_factory):
            detail, produced, safety = body()
    except AuthorityBusyError:
        log.append(
            operation=verb,
            outcome="refused",
            db_identity=identity,
            detail={"reason": "authority_lock_held"},
        )
        raise
    except BreakGlassError as exc:
        log.append(
            operation=verb,
            outcome="failed",
            db_identity=identity,
            detail={"reason": type(exc).__name__},
        )
        raise
    except Exception as exc:  # noqa: BLE001 - audit then re-raise
        log.append(
            operation=verb,
            outcome="failed",
            db_identity=identity,
            detail={"reason": type(exc).__name__},
        )
        raise

    entry = log.append(
        operation=verb, outcome="succeeded", db_identity=identity, detail=detail
    )
    return BreakGlassReport(
        operation=verb,
        db_identity=identity,
        outcome="succeeded",
        detail=detail,
        audit_event_id=entry.event_id,
        produced_path=produced,
        safety_backup_path=safety,
    )


def backup_database(
    db_path: str | Path,
    destination: str | Path,
    *,
    confirm: str | None,
    audit_log: OperatorAuditLog | None = None,
    lock_factory: Callable[[Path], Any] | None = None,
) -> BreakGlassReport:
    """Whole-database snapshot through the SQLite online backup API."""

    source = Path(db_path).expanduser()
    target = Path(destination)

    def _body() -> tuple[dict[str, Any], Path | None, Path | None]:
        if not source.is_file():
            raise SourceRefusedError("there is no authority database at the configured location")
        checked = _check_new_destination(target)
        before = provisioning.database_status(source)
        if not before.integrity_ok:
            raise BackupVerificationError(
                "the live authority database failed its integrity check; "
                "refusing to snapshot a corrupt database"
            )
        migrations = _load_migrations()
        migrations.sqlite_backup(source, checked)
        provisioning.harden_file(checked)
        verified = verify_backup(checked)
        if not verified.ready or verified.schema_version != before.schema_version:
            checked.unlink(missing_ok=True)
            raise BackupVerificationError("the snapshot failed verification and was removed")
        return (
            {
                "schema_version": verified.schema_version,
                "memory_rows": verified.memory_count,
                "integrity_ok": verified.integrity_ok,
                "snapshot_bytes": checked.stat().st_size,
                "target_identity": db_identity(checked),
                "method": "sqlite_online_backup_api",
            },
            checked,
            None,
        )

    return _run_guarded(
        "backup",
        source,
        confirm=confirm,
        audit_log=audit_log,
        lock_factory=lock_factory,
        body=_body,
    )


def restore_whole_database(
    db_path: str | Path,
    source_backup: str | Path,
    *,
    whole_database: bool,
    confirm: str | None,
    audit_log: OperatorAuditLog | None = None,
    lock_factory: Callable[[Path], Any] | None = None,
) -> BreakGlassReport:
    """Replace the entire authority database from a verified snapshot.

    ``whole_database`` must be ``True``: there is deliberately no row-level
    restore on this code path (task 6.10c).
    """

    target = Path(db_path).expanduser()
    backup = Path(source_backup)

    def _body() -> tuple[dict[str, Any], Path | None, Path | None]:
        if not whole_database:
            raise OfflineOperationDeniedError(
                "row-restore", OFFLINE_DENIED_OPERATIONS["row-restore"]
            )
        checked_source = _check_existing_source(backup)
        _refuse_symlink(target, error=DestinationRefusedError, label="authority database")

        verified = verify_backup(checked_source)
        if not verified.ready:
            raise BackupVerificationError(
                "the snapshot failed integrity/schema verification; refusing to restore it"
            )
        supported = _supported_schema_version()
        if verified.schema_version > supported:
            raise BackupVerificationError(
                "the snapshot was written by a newer schema version than this "
                "installation supports; upgrade recall-memory-mcp before restoring"
            )

        migrations = _load_migrations()

        # Re-backup the *current* database before replacing it (design.md §9.1).
        safety: Path | None = None
        if target.is_file():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safety = target.with_name(f"{target.name}.pre-restore-{stamp}.bak")
            safety = _check_new_destination(safety)
            migrations.sqlite_backup(target, safety)
            provisioning.harden_file(safety)
            safety_check = verify_backup(safety)
            if not safety_check.integrity_ok:
                safety.unlink(missing_ok=True)
                raise BackupVerificationError(
                    "the pre-restore safety snapshot failed verification; nothing was replaced"
                )

        # Replace the live database *through the SQLite online backup API*
        # rather than by copying a file over it.  ``Connection.backup``
        # overwrites the whole destination database transactionally, so the
        # target never spends a moment as a torn file and its WAL/SHM
        # sidecars stay consistent.
        try:
            _overwrite_via_backup_api(checked_source, target)
        except BaseException:
            if safety is not None and safety.is_file():
                _overwrite_via_backup_api(safety, target)
            raise
        provisioning.harden_database(target)

        read_back = verify_backup(target)
        if not read_back.ready or read_back.schema_version != verified.schema_version:
            raise BackupVerificationError("read-back of the restored database failed")
        return (
            {
                "schema_version": read_back.schema_version,
                "memory_rows": read_back.memory_count,
                "integrity_ok": read_back.integrity_ok,
                "source_identity": db_identity(checked_source),
                "safety_snapshot_taken": safety is not None,
                "method": "sqlite_online_backup_api",
                "scope": "whole_database",
            },
            target,
            safety,
        )

    return _run_guarded(
        "restore",
        target,
        confirm=confirm,
        audit_log=audit_log,
        lock_factory=lock_factory,
        body=_body,
    )


def migrate_database(
    db_path: str | Path,
    *,
    confirm: str | None,
    owner_id: str | None = None,
    backup_path: str | Path | None = None,
    audit_log: OperatorAuditLog | None = None,
    lock_factory: Callable[[Path], Any] | None = None,
) -> BreakGlassReport:
    """Run the canonical migration runner with a verified pre-migration backup."""

    target = Path(db_path).expanduser()

    def _body() -> tuple[dict[str, Any], Path | None, Path | None]:
        if not target.is_file():
            raise SourceRefusedError("there is no authority database at the configured location")
        before = provisioning.database_status(target)
        if not before.integrity_ok:
            raise BackupVerificationError(
                "the authority database failed its integrity check; refusing to migrate it"
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot = (
            Path(backup_path)
            if backup_path is not None
            else target.with_name(f"{target.name}.pre-migrate-{stamp}.bak")
        )
        checked_snapshot = _check_new_destination(snapshot)

        migrations = _load_migrations()
        resolved_owner = owner_id or _existing_owner_id(target) or f"owner-{uuid.uuid4().hex[:16]}"
        bootstrap = migrations.BootstrapIdentity(
            owner_id=resolved_owner,
            migration_grant_id=f"grant-migration-{resolved_owner}",
            created_at=_utc_now(),
        )
        report = migrations.migrate_database(
            target, backup_path=checked_snapshot, bootstrap=bootstrap
        )
        provisioning.harden_file(checked_snapshot)
        provisioning.harden_database(target)

        read_back = verify_backup(target)
        if not read_back.ready or read_back.schema_version != report.to_version:
            raise BackupVerificationError("read-back of the migrated database failed")
        return (
            {
                "from_version": report.from_version,
                "to_version": report.to_version,
                "applied_versions": list(report.applied_versions),
                "memory_rows": read_back.memory_count,
                "integrity_ok": read_back.integrity_ok,
                "pre_migration_snapshot": True,
            },
            target,
            checked_snapshot,
        )

    return _run_guarded(
        "migrate",
        target,
        confirm=confirm,
        audit_log=audit_log,
        lock_factory=lock_factory,
        body=_body,
    )


def _existing_owner_id(db_path: Path) -> str | None:
    """Reuse the database's own owner id so migration invents no identity."""

    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT owner_id FROM owners ORDER BY created_at LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return str(row[0]) if row else None


def audit_entries(db_path: str | Path) -> Iterable[AuditEntry]:
    """Convenience read-back of the sidecar operator audit for ``db_path``."""

    return OperatorAuditLog(audit_path_for(db_path)).entries()
