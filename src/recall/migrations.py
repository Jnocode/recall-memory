"""Versioned SQLite migrations for the canonical Recall store.

This module intentionally operates on an explicit database path.  It never
consults the user's default Recall path, and it uses SQLite's backup API rather
than copying a potentially live WAL database as ordinary files.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


LEGACY_SCOPE = "legacy:unscoped"
LATEST_SCHEMA_VERSION = 1


class MigrationError(RuntimeError):
    """A migration could not be safely applied or verified."""


class MigrationVerificationError(MigrationError):
    """The migrated database failed durable read-back verification."""


@dataclass(frozen=True)
class BootstrapIdentity:
    """Stable local identity used to quarantine pre-MCP rows.

    The authority initializer supplies these values.  A migration never invents
    an OAuth identity or treats this restricted migration grant as a token.
    """

    owner_id: str
    migration_grant_id: str
    created_at: str


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection, BootstrapIdentity], None]


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied_versions: tuple[int, ...]
    backup_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def open_migration_connection(
    db_path: str | Path, *, readonly: bool = False
) -> sqlite3.Connection:
    """Open a migration connection with foreign-key enforcement enabled."""

    path = Path(db_path)
    if readonly:
        conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def sqlite_backup(source_path: str | Path, destination_path: str | Path) -> Path:
    """Create a pre-migration SQLite-consistent snapshot.

    The destination is exclusive: an existing file is never overwritten.
    """

    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file():
        raise FileNotFoundError(f"Source database does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_conn = open_migration_connection(source, readonly=True)
    destination_conn = sqlite3.connect(destination, timeout=30.0)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    except BaseException:
        destination_conn.close()
        source_conn.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        destination_conn.close()
        source_conn.close()

    # ``closing`` matters: ``sqlite3.Connection.__exit__`` only ends the
    # transaction, it does NOT close the connection.  A leaked read-only
    # connection keeps the ``-wal`` sidecar open, which breaks whole-database
    # break-glass operations on Windows.
    with closing(open_migration_connection(destination, readonly=True)) as reopened:
        integrity = reopened.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            destination.unlink(missing_ok=True)
            raise MigrationVerificationError("SQLite backup integrity check failed")
    return destination


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def current_schema_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def _validate_bootstrap(bootstrap: BootstrapIdentity) -> None:
    if not bootstrap.owner_id.strip():
        raise MigrationError("Bootstrap owner_id must not be empty")
    if not bootstrap.migration_grant_id.strip():
        raise MigrationError("Bootstrap migration_grant_id must not be empty")
    if not bootstrap.created_at.strip():
        raise MigrationError("Bootstrap created_at must not be empty")


def _rebuild_legacy_fts(conn: sqlite3.Connection) -> None:
    """Create or transactionally rebuild the canonical derived FTS index."""

    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, id UNINDEXED, tokenize='porter unicode61'
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(memories_fts)").fetchall()
    }
    if not {"content", "id"} <= columns:
        raise MigrationError("Canonical legacy FTS schema is incompatible")
    conn.execute("DELETE FROM memories_fts")
    conn.execute("INSERT INTO memories_fts(content, id) SELECT content, id FROM memories")


def _create_shared_memory_schema(
    conn: sqlite3.Connection, bootstrap: BootstrapIdentity
) -> None:
    _rebuild_legacy_fts(conn)
    statements = (
        """
        CREATE TABLE owners (
            owner_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE owner_link_challenges (
            challenge_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            challenge_hash TEXT NOT NULL,
            oauth_state_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
        """,
        """
        CREATE TABLE client_grants (
            grant_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            issuer TEXT NOT NULL,
            subject TEXT NOT NULL,
            client_id TEXT NOT NULL,
            grant_generation INTEGER NOT NULL CHECK(grant_generation >= 1),
            grant_kind TEXT NOT NULL CHECK(grant_kind IN ('OAUTH', 'MIGRATION')),
            source_client TEXT NOT NULL,
            oauth_scopes_json TEXT NOT NULL,
            memory_scope_patterns_json TEXT NOT NULL,
            binding_challenge_id TEXT REFERENCES owner_link_challenges(challenge_id)
                ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            reauthorized_at TEXT,
            revoked_at TEXT,
            unlinked_at TEXT,
            UNIQUE(issuer, subject, client_id, grant_generation),
            UNIQUE(grant_id, owner_id)
        )
        """,
        """
        CREATE TABLE owner_link_events (
            link_event_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            grant_id TEXT,
            challenge_id TEXT REFERENCES owner_link_challenges(challenge_id)
                ON DELETE RESTRICT,
            operation TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            details_digest TEXT NOT NULL,
            FOREIGN KEY(grant_id, owner_id)
                REFERENCES client_grants(grant_id, owner_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE embedding_profiles (
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            scope TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation >= 1),
            provider TEXT NOT NULL,
            endpoint_identity_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL CHECK(dimension >= 1),
            status TEXT NOT NULL
                CHECK(status IN ('BUILDING','ACTIVE','RETIRED','FAILED')),
            created_at TEXT NOT NULL,
            activated_at TEXT,
            retired_at TEXT,
            PRIMARY KEY(owner_id, scope, generation)
        )
        """,
        """
        CREATE UNIQUE INDEX idx_embedding_profiles_one_active
            ON embedding_profiles(owner_id, scope)
            WHERE status = 'ACTIVE'
        """,
        """
        CREATE TABLE memory_metadata (
            memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE RESTRICT,
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            creator_grant_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision >= 1),
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_client TEXT NOT NULL,
            source_conversation TEXT,
            actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            reindex_required INTEGER NOT NULL DEFAULT 0
                CHECK(reindex_required IN (0, 1)),
            embedding_provenance_json TEXT,
            UNIQUE(memory_id, owner_id, scope),
            FOREIGN KEY(creator_grant_id, owner_id)
                REFERENCES client_grants(grant_id, owner_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX idx_memory_metadata_visibility
            ON memory_metadata(owner_id, scope, deleted_at, updated_at DESC)
        """,
        """
        CREATE TABLE memory_embeddings (
            embedding_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            generation INTEGER NOT NULL,
            provider TEXT NOT NULL,
            endpoint_identity_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL CHECK(dimension >= 1),
            embedding_blob BLOB NOT NULL,
            vector_rowid INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(memory_id, owner_id, scope)
                REFERENCES memory_metadata(memory_id, owner_id, scope)
                ON DELETE RESTRICT,
            FOREIGN KEY(owner_id, scope, generation)
                REFERENCES embedding_profiles(owner_id, scope, generation)
                ON DELETE CASCADE,
            UNIQUE(memory_id, generation)
        )
        """,
        """
        CREATE INDEX idx_memory_embeddings_owner_scope_gen
            ON memory_embeddings(owner_id, scope, generation)
        """,
        """
        CREATE TABLE vector_tables (
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            scope TEXT NOT NULL,
            generation INTEGER NOT NULL,
            table_name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(owner_id, scope, generation)
                REFERENCES embedding_profiles(owner_id, scope, generation)
                ON DELETE CASCADE,
            UNIQUE(owner_id, scope, generation)
        )
        """,
        """
        CREATE TABLE memory_events (
            event_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            grant_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision >= 1),
            operation TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            source_client TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            UNIQUE(memory_id, revision),
            FOREIGN KEY(grant_id, owner_id)
                REFERENCES client_grants(grant_id, owner_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE mcp_idempotency (
            owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
            grant_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            result_schema_version INTEGER NOT NULL CHECK(result_schema_version >= 1),
            payload_digest TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            purged_at TEXT,
            PRIMARY KEY(grant_id, operation, idempotency_key),
            FOREIGN KEY(grant_id, owner_id)
                REFERENCES client_grants(grant_id, owner_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX idx_mcp_idempotency_memory
            ON mcp_idempotency(owner_id, memory_id)
        """,
    )
    for statement in statements:
        conn.execute(statement)

    conn.execute(
        "INSERT INTO owners(owner_id, created_at) VALUES (?, ?)",
        (bootstrap.owner_id, bootstrap.created_at),
    )
    conn.execute(
        """
        INSERT INTO client_grants (
            grant_id, owner_id, issuer, subject, client_id, grant_generation,
            grant_kind, source_client, oauth_scopes_json,
            memory_scope_patterns_json, binding_challenge_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bootstrap.migration_grant_id,
            bootstrap.owner_id,
            "local-migration",
            bootstrap.owner_id,
            "legacy-import",
            1,
            "MIGRATION",
            "legacy-import",
            json.dumps([], separators=(",", ":")),
            json.dumps([LEGACY_SCOPE], separators=(",", ":")),
            None,
            bootstrap.created_at,
        ),
    )

    memory_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    session_expression = "session_id" if "session_id" in memory_columns else "NULL"
    rows = conn.execute(
        f"SELECT id, content, timestamp, embedding, {session_expression} "
        "FROM memories ORDER BY id"
    ).fetchall()
    for memory_id, content, timestamp, embedding, session_id in rows:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO memory_metadata (
                memory_id, owner_id, creator_grant_id, revision, scope, kind,
                tags_json, content_hash, source_client, source_conversation,
                actor_id, created_at, updated_at, deleted_at,
                reindex_required, embedding_provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                bootstrap.owner_id,
                bootstrap.migration_grant_id,
                1,
                LEGACY_SCOPE,
                "legacy",
                "[]",
                content_hash,
                "legacy-import",
                session_id if isinstance(session_id, str) and session_id.strip() else None,
                "legacy-import",
                timestamp,
                timestamp,
                None,
                1 if embedding is not None else 0,
                None,
            ),
        )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "mcp_shared_memory_schema", _create_shared_memory_schema),
)


def _validate_registry(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions = [migration.version for migration in ordered]
    if not ordered or versions != list(range(1, len(ordered) + 1)):
        raise MigrationError("Migration versions must be unique and contiguous from 1")
    if any(not migration.name.strip() for migration in ordered):
        raise MigrationError("Migration names must not be empty")
    return ordered


def run_migrations(
    conn: sqlite3.Connection,
    *,
    bootstrap: BootstrapIdentity,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> tuple[int, tuple[int, ...]]:
    """Apply pending migrations inside the caller's active transaction."""

    _validate_bootstrap(bootstrap)
    ordered = _validate_registry(migrations)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied_rows = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = {int(version): name for version, name in applied_rows}

    known = {migration.version: migration.name for migration in ordered}
    for version, name in applied.items():
        if version not in known or known[version] != name:
            raise MigrationError("Applied migration history does not match this build")

    current = max(applied, default=0)
    newly_applied: list[int] = []
    for migration in ordered:
        if migration.version <= current:
            continue
        if migration.version != current + 1:
            raise MigrationError("Migration history contains a version gap")
        migration.apply(conn, bootstrap)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, _utc_now()),
        )
        current = migration.version
        newly_applied.append(migration.version)

    conn.execute(f"PRAGMA user_version={current}")
    return current, tuple(newly_applied)


def _legacy_fingerprint(conn: sqlite3.Connection) -> tuple[int, str]:
    if not _table_exists(conn, "memories"):
        raise MigrationError("Canonical legacy memories table is missing")
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    if not {"id", "content", "timestamp", "embedding"} <= columns:
        raise MigrationError("Canonical legacy memories schema is incompatible")

    digest = hashlib.sha256()
    rows = conn.execute("SELECT id, content FROM memories ORDER BY id").fetchall()
    for memory_id, content in rows:
        for value in (memory_id, content):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return len(rows), digest.hexdigest()


def _index_details(
    conn: sqlite3.Connection, table: str, index_name: str
) -> tuple[bool, bool, tuple[str, ...], str] | None:
    """Return (unique, partial, columns, SQL) for a trusted manifest name."""

    rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    for row in rows:
        if row[1] != index_name:
            continue
        columns = tuple(
            item[2]
            for item in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        )
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        return bool(row[2]), bool(row[4]), columns, (sql_row[0] if sql_row else "")
    return None


def _has_unique_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> bool:
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not row[2]:
            continue
        index_columns = tuple(
            item[2]
            for item in conn.execute(f'PRAGMA index_info("{row[1]}")').fetchall()
        )
        if index_columns == columns:
            return True
    return False


def _foreign_key_signatures(
    conn: sqlite3.Connection, table: str
) -> set[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    grouped: dict[int, list[tuple[object, ...]]] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        grouped.setdefault(int(row[0]), []).append(row)
    signatures: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        signatures.add(
            (
                str(ordered[0][2]),
                tuple(str(row[3]) for row in ordered),
                tuple(str(row[4]) for row in ordered),
            )
        )
    return signatures


def _verify_schema_manifest(conn: sqlite3.Connection) -> None:
    required_columns = {
        "owners": {"owner_id", "created_at"},
        "client_grants": {
            "grant_id",
            "owner_id",
            "grant_generation",
            "grant_kind",
            "source_client",
            "oauth_scopes_json",
            "memory_scope_patterns_json",
        },
        "owner_link_challenges": {
            "challenge_id",
            "owner_id",
            "challenge_hash",
            "oauth_state_hash",
            "expires_at",
            "used_at",
        },
        "owner_link_events": {
            "link_event_id",
            "owner_id",
            "grant_id",
            "challenge_id",
            "operation",
            "actor_id",
            "details_digest",
        },
        "embedding_profiles": {
            "owner_id",
            "scope",
            "generation",
            "provider",
            "endpoint_identity_hash",
            "model",
            "dimension",
            "status",
        },
        "memory_embeddings": {
            "embedding_id",
            "memory_id",
            "owner_id",
            "scope",
            "generation",
            "embedding_blob",
        },
        "vector_tables": {"owner_id", "scope", "generation", "table_name"},
        "memory_metadata": {
            "memory_id",
            "owner_id",
            "creator_grant_id",
            "revision",
            "scope",
            "kind",
            "content_hash",
            "source_client",
            "actor_id",
            "deleted_at",
        },
        "memory_events": {
            "event_id",
            "memory_id",
            "owner_id",
            "grant_id",
            "revision",
            "operation",
            "payload_digest",
        },
        "mcp_idempotency": {
            "owner_id",
            "grant_id",
            "operation",
            "idempotency_key",
            "memory_id",
            "scope",
            "result_schema_version",
            "payload_digest",
            "result_json",
            "purged_at",
        },
    }
    for table, expected in required_columns.items():
        actual = {
            row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        if not expected <= actual:
            raise MigrationVerificationError(
                f"Required columns are missing from schema table {table}"
            )

    active = _index_details(
        conn, "embedding_profiles", "idx_embedding_profiles_one_active"
    )
    if active is None or active[:3] != (True, True, ("owner_id", "scope")):
        raise MigrationVerificationError("Active-profile index definition is invalid")
    normalized_sql = re.sub(r"\s+", " ", active[3]).strip()
    if not re.search(r"WHERE\s+status\s*=\s*'ACTIVE'", normalized_sql, re.IGNORECASE):
        raise MigrationVerificationError("Active-profile index predicate is invalid")

    covering = _index_details(
        conn, "memory_embeddings", "idx_memory_embeddings_owner_scope_gen"
    )
    if covering is None or covering[:3] != (
        False,
        False,
        ("owner_id", "scope", "generation"),
    ):
        raise MigrationVerificationError("Embedding covering index definition is invalid")

    idempotency = _index_details(
        conn, "mcp_idempotency", "idx_mcp_idempotency_memory"
    )
    if idempotency is None or idempotency[:3] != (
        False,
        False,
        ("owner_id", "memory_id"),
    ):
        raise MigrationVerificationError("Idempotency memory index definition is invalid")

    required_unique = {
        "memory_embeddings": (("memory_id", "generation"),),
        "memory_events": (("memory_id", "revision"),),
        "mcp_idempotency": (("grant_id", "operation", "idempotency_key"),),
    }
    for table, signatures in required_unique.items():
        for signature in signatures:
            if not _has_unique_columns(conn, table, signature):
                raise MigrationVerificationError(
                    f"Required unique constraint is missing from schema table {table}"
                )

    required_foreign_keys = {
        "memory_metadata": {
            ("client_grants", ("creator_grant_id", "owner_id"), ("grant_id", "owner_id"))
        },
        "memory_embeddings": {
            (
                "memory_metadata",
                ("memory_id", "owner_id", "scope"),
                ("memory_id", "owner_id", "scope"),
            ),
            (
                "embedding_profiles",
                ("owner_id", "scope", "generation"),
                ("owner_id", "scope", "generation"),
            ),
        },
        "memory_events": {
            ("client_grants", ("grant_id", "owner_id"), ("grant_id", "owner_id"))
        },
        "mcp_idempotency": {
            ("client_grants", ("grant_id", "owner_id"), ("grant_id", "owner_id"))
        },
    }
    for table, expected in required_foreign_keys.items():
        if not expected <= _foreign_key_signatures(conn, table):
            raise MigrationVerificationError(
                f"Required foreign key is missing from schema table {table}"
            )


def verify_migrated_database(conn: sqlite3.Connection) -> None:
    """Verify schema manifest, FK integrity, and migration provenance."""

    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        raise MigrationVerificationError("SQLite integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationVerificationError("SQLite foreign-key check failed")

    required_tables = {
        "schema_migrations",
        "memories_fts",
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
    }
    actual_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = required_tables - actual_tables
    if missing_tables:
        raise MigrationVerificationError("Required migration tables are missing")

    fts_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(memories_fts)").fetchall()
    }
    if not {"content", "id"} <= fts_columns:
        raise MigrationVerificationError("Canonical FTS columns are missing")

    _verify_schema_manifest(conn)

    memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    metadata_count = conn.execute("SELECT COUNT(*) FROM memory_metadata").fetchone()[0]
    if memory_count != metadata_count:
        raise MigrationVerificationError("Memory metadata count mismatch")

    migration_rows = conn.execute(
        """
        SELECT mm.revision, mm.scope, mm.kind, mm.source_client,
               mm.content_hash, m.content
        FROM memory_metadata AS mm
        JOIN client_grants AS cg
          ON cg.grant_id = mm.creator_grant_id
         AND cg.owner_id = mm.owner_id
        JOIN memories AS m ON m.id = mm.memory_id
        WHERE cg.grant_kind = 'MIGRATION'
        """
    ).fetchall()
    for revision, scope, kind, source_client, content_hash, content in migration_rows:
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            revision != 1
            or scope != LEGACY_SCOPE
            or kind != "legacy"
            or source_client != "legacy-import"
            or content_hash != expected_hash
        ):
            raise MigrationVerificationError(
                "Legacy memory quarantine metadata is invalid"
            )


def migrate_database(
    db_path: str | Path,
    *,
    backup_path: str | Path,
    bootstrap: BootstrapIdentity,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> MigrationReport:
    """Back up, transactionally migrate, and independently reopen/read back."""

    source = Path(db_path)
    backup = sqlite_backup(source, backup_path)

    with closing(open_migration_connection(source, readonly=True)) as before_conn:
        before_fingerprint = _legacy_fingerprint(before_conn)
        from_version = current_schema_version(before_conn)

    conn = open_migration_connection(source)
    try:
        conn.execute("BEGIN IMMEDIATE")
        to_version, applied_versions = run_migrations(
            conn, bootstrap=bootstrap, migrations=migrations
        )
        verify_migrated_database(conn)
        if _legacy_fingerprint(conn) != before_fingerprint:
            raise MigrationVerificationError("Legacy IDs or content changed during migration")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    with closing(open_migration_connection(source, readonly=True)) as reopened:
        verify_migrated_database(reopened)
        if _legacy_fingerprint(reopened) != before_fingerprint:
            raise MigrationVerificationError("Reopen legacy read-back did not match")
        durable_version = current_schema_version(reopened)
        if durable_version != to_version:
            raise MigrationVerificationError("Reopen schema version did not match")
        if reopened.execute("PRAGMA user_version").fetchone()[0] != to_version:
            raise MigrationVerificationError("SQLite user_version did not match")

    return MigrationReport(
        from_version=from_version,
        to_version=to_version,
        applied_versions=applied_versions,
        backup_path=backup,
    )
