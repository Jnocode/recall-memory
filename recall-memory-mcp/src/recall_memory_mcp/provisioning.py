"""Local authority provisioning and diagnostics (tasks 6.2 and 6.4).

Everything here is *local operator* territory: it creates the on-disk
config file and the authority SQLite database, and it inspects them for
``doctor``.  Two rules are absolute:

* **Never overwrite.**  ``init`` refuses if either target already exists
  (R9.2); an authority database is not a file we are allowed to guess about.
* **Never fabricate a schema.**  The canonical migration runner in
  ``recall.migrations`` owns the schema.  If the canonical core is not
  installed we fail closed instead of inventing tables.

Path values are *printed by ``init``* because task 6.2 requires the operator
to see exactly what will be created before it is created.  They are never
returned to a client and ``doctor`` redacts them (R8.5).
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

CONFIG_SCHEMA_VERSION: Final[int] = 1

#: Tables that a fully provisioned authority database must expose.  The list
#: is asserted against a freshly migrated database by the CLI test-suite, so
#: it cannot silently drift away from ``recall.migrations``.
REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "schema_migrations",
    "memories",
    "memories_fts",
    "keywords",
    "owners",
    "client_grants",
    "owner_link_challenges",
    "owner_link_events",
    "embedding_profiles",
    "memory_embeddings",
    "vector_tables",
    "memory_metadata",
    "memory_events",
    "mcp_idempotency",
)


class ProvisioningError(RuntimeError):
    """The local authority could not be created or inspected safely."""


class AuthorityCoreUnavailableError(ProvisioningError):
    """The canonical ``recall-sqlite`` core is not importable."""


class RefusedOverwriteError(ProvisioningError):
    """A target file already exists; ``init`` never overwrites (R9.2)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _load_store() -> Any:
    try:
        from recall.store import SQLiteStore  # noqa: PLC0415 - deliberately lazy
    except Exception as exc:  # noqa: BLE001
        raise AuthorityCoreUnavailableError(
            "the canonical recall-sqlite core is not installed"
        ) from exc
    return SQLiteStore


def _run_migrations(conn: sqlite3.Connection, *, bootstrap: Any) -> tuple[int, tuple[int, ...]]:
    """Seam that the test-suite patches to prove rollback (task 1.12 pattern)."""

    migrations = _load_migrations()
    return migrations.run_migrations(conn, bootstrap=bootstrap)


# ---------------------------------------------------------------------------
# owner-only permissions (R8.6)
# ---------------------------------------------------------------------------


def harden_file(path: Path) -> bool:
    """Restrict ``path`` to the current OS user. Returns the honest result."""

    if not path.exists():
        return False
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            return False
        return path.stat().st_mode & 0o777 == 0o600

    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(F)",
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def sidecar_paths(db_path: str | Path) -> tuple[Path, ...]:
    """WAL/SHM companions. They hold memory content and must be protected."""

    path = Path(db_path)
    return (
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    )


def harden_database(db_path: str | Path) -> bool:
    """Harden the database *and* every existing WAL/SHM sidecar (R8.6)."""

    path = Path(db_path)
    results = [harden_file(path)]
    for sidecar in sidecar_paths(path):
        if sidecar.exists():
            results.append(harden_file(sidecar))
    return all(results)


# ---------------------------------------------------------------------------
# config file
# ---------------------------------------------------------------------------

_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise ProvisioningError(f"unsupported config value type: {type(value).__name__}")


def render_config(document: dict[str, Any]) -> str:
    """Serialise a shallow ``{key: scalar | {key: scalar}}`` mapping as TOML."""

    lines = [
        "# recall-memory-mcp local configuration",
        "# Generated by `recall-memory-mcp init`.",
        "# Secrets never belong in this file: pass them through the",
        "# RECALL_MCP_* environment instead.",
        "",
    ]
    for key, value in document.items():
        if isinstance(value, dict):
            continue
        if not _TOML_KEY.match(key):
            raise ProvisioningError(f"unsupported config key: {key!r}")
        lines.append(f"{key} = {_toml_value(value)}")
    for key, value in document.items():
        if not isinstance(value, dict):
            continue
        if not _TOML_KEY.match(key):
            raise ProvisioningError(f"unsupported config key: {key!r}")
        lines.append("")
        lines.append(f"[{key}]")
        for sub_key, sub_value in value.items():
            if not _TOML_KEY.match(sub_key):
                raise ProvisioningError(f"unsupported config key: {sub_key!r}")
            lines.append(f"{sub_key} = {_toml_value(sub_value)}")
    return "\n".join(lines) + "\n"


def _parse_config(text: str) -> dict[str, Any]:
    """Minimal reader for the exact subset :func:`render_config` emits."""

    document: dict[str, Any] = {}
    table: dict[str, Any] = document
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            table = document.setdefault(name, {})
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value == "true":
            table[key] = True
        elif value == "false":
            table[key] = False
        elif value.startswith('"') and value.endswith('"'):
            table[key] = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            try:
                table[key] = int(value)
            except ValueError:
                table[key] = value
    return document


def read_config(path: str | Path) -> dict[str, Any]:
    """Read a generated config file. Uses ``tomllib`` when it is available."""

    text = Path(path).read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib  # noqa: PLC0415 - stdlib on 3.11+

        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ProvisioningError("configuration file is not valid TOML") from exc
    return _parse_config(text)


# ---------------------------------------------------------------------------
# database status (doctor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseStatus:
    exists: bool
    schema_version: int
    missing_tables: tuple[str, ...]
    integrity_ok: bool
    error: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.exists
            and self.integrity_ok
            and not self.missing_tables
            and self.schema_version >= 1
            and self.error is None
        )


def database_status(db_path: str | Path) -> DatabaseStatus:
    """Inspect the authority database without mutating a single row."""

    path = Path(db_path)
    if not path.is_file():
        return DatabaseStatus(
            exists=False, schema_version=0, missing_tables=REQUIRED_TABLES, integrity_ok=False
        )

    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        return DatabaseStatus(
            exists=True,
            schema_version=0,
            missing_tables=REQUIRED_TABLES,
            integrity_ok=False,
            error=type(exc).__name__,
        )
    try:
        conn.execute("PRAGMA query_only=ON")
        integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = bool(integrity_row) and integrity_row[0] == "ok"
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = tuple(name for name in REQUIRED_TABLES if name not in names)
        if "schema_migrations" in names:
            version = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            )
        else:
            version = 0
    except sqlite3.DatabaseError as exc:
        return DatabaseStatus(
            exists=True,
            schema_version=0,
            missing_tables=REQUIRED_TABLES,
            integrity_ok=False,
            error=type(exc).__name__,
        )
    finally:
        conn.close()

    return DatabaseStatus(
        exists=True,
        schema_version=version,
        missing_tables=missing,
        integrity_ok=integrity_ok,
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitReport:
    config_path: Path
    db_path: Path
    schema_version: int
    owner_id: str
    config_owner_only: bool
    db_owner_only: bool


def _create_authority_database(db_path: Path, *, owner_id: str, now: str) -> int:
    migrations = _load_migrations()
    store_cls = _load_store()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    created_paths = [db_path, *sidecar_paths(db_path)]

    def _cleanup() -> None:
        for candidate in created_paths:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        # 1. canonical base store schema (memories/keywords/vector tables)
        store_cls(str(db_path))

        # 2. versioned MCP schema, inside a single immediate transaction
        bootstrap = migrations.BootstrapIdentity(
            owner_id=owner_id,
            migration_grant_id=f"grant-migration-{owner_id}",
            created_at=now,
        )
        conn = migrations.open_migration_connection(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            version, _applied = _run_migrations(conn, bootstrap=bootstrap)
            migrations.verify_migrated_database(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

        # 3. independent reopen read-back (task 6.2).  ``closing`` matters:
        # ``Connection.__exit__`` ends the transaction but never closes the
        # connection, and a leaked reader keeps the ``-wal`` sidecar open.
        with contextlib.closing(
            migrations.open_migration_connection(db_path, readonly=True)
        ) as reopened:
            migrations.verify_migrated_database(reopened)
            durable = migrations.current_schema_version(reopened)
            if durable != version:
                raise ProvisioningError("read-back schema version did not match")
    except BaseException:
        _cleanup()
        raise

    status = database_status(db_path)
    if not status.ready:
        _cleanup()
        raise ProvisioningError("read-back verification of the new authority database failed")
    return status.schema_version


def initialize(settings: Any, *, owner_id: str | None = None, now: str | None = None) -> InitReport:
    """Create the local config file and authority database exactly once."""

    config_path = Path(settings.config_path)
    db_path = Path(settings.db_path)
    if config_path.exists():
        raise RefusedOverwriteError("a configuration file already exists at the target location")
    if db_path.exists():
        raise RefusedOverwriteError("an authority database already exists at the target location")

    resolved_owner = owner_id or f"owner-{uuid.uuid4().hex[:16]}"
    timestamp = now or _utc_now()

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(config_path.parent, 0o700)
        except OSError:
            pass

    schema_version = _create_authority_database(db_path, owner_id=resolved_owner, now=timestamp)

    document = {
        "schema": CONFIG_SCHEMA_VERSION,
        "created_at": timestamp,
        "owner_id": resolved_owner,
        "server": {
            "mode": settings.mode.value,
            "host": settings.host,
            "port": settings.port,
        },
        "database": {
            "path": str(db_path),
            "schema_version": schema_version,
        },
    }
    try:
        with open(config_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(render_config(document))
    except FileExistsError as exc:
        raise RefusedOverwriteError("a configuration file appeared during init") from exc

    return InitReport(
        config_path=config_path,
        db_path=db_path,
        schema_version=schema_version,
        owner_id=resolved_owner,
        config_owner_only=harden_file(config_path),
        db_owner_only=harden_database(db_path),
    )


__all__ = [
    "AuthorityCoreUnavailableError",
    "CONFIG_SCHEMA_VERSION",
    "DatabaseStatus",
    "InitReport",
    "ProvisioningError",
    "REQUIRED_TABLES",
    "RefusedOverwriteError",
    "database_status",
    "harden_database",
    "harden_file",
    "initialize",
    "read_config",
    "render_config",
]
