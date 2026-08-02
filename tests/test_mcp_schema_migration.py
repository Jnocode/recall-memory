"""Integration tests for the Recall MCP versioned SQLite migration.

The fixtures deliberately create the tracked legacy schema directly.  They do
not instantiate ``SQLiteStore`` because its constructor mutates the database
before the migration can take a consistent backup.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recall.migrations import (  # noqa: E402
    BootstrapIdentity,
    Migration,
    MigrationVerificationError,
    migrate_database,
)
from recall.mcp_repository import RecallMCPRepository  # noqa: E402


LEGACY_ROWS = (
    (
        "legacy-alpha",
        "原始 Recall 記憶：docker-compose preference",
        '["docker-compose"]',
        "2026-01-02T03:04:05+00:00",
        json.dumps([0.25, 0.5]),
        7,
        "",
        "semantic",
        "hot",
        None,
        None,
    ),
    (
        "legacy-beta",
        "Second legacy memory — content must remain byte-for-byte stable.",
        "[]",
        "2026-02-03T04:05:06+00:00",
        None,
        0,
        "old-session",
        "episodic",
        "warm",
        None,
        "2026-02-04T00:00:00+00:00",
    ),
)


@pytest.fixture
def bootstrap() -> BootstrapIdentity:
    return BootstrapIdentity(
        owner_id="owner-local-test",
        migration_grant_id="grant-legacy-import-test",
        created_at="2026-08-02T00:00:00+00:00",
    )


def _create_legacy_db(path: Path, *, with_fts: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                entities TEXT DEFAULT '[]',
                timestamp TEXT NOT NULL,
                embedding BLOB,
                access_count INTEGER DEFAULT 0,
                session_id TEXT DEFAULT '',
                tag TEXT DEFAULT 'episodic',
                tier TEXT DEFAULT 'hot',
                last_accessed_at TEXT,
                last_demoted_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE keywords (
                keyword TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                PRIMARY KEY (keyword, memory_id)
            )
            """
        )
        conn.execute("CREATE INDEX idx_kw ON keywords(keyword)")
        if with_fts:
            conn.execute(
                """
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    content, id UNINDEXED, tokenize='porter unicode61'
                )
                """
            )
        conn.executemany(
            """
            INSERT INTO memories (
                id, content, entities, timestamp, embedding, access_count,
                session_id, tag, tier, last_accessed_at, last_demoted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            LEGACY_ROWS,
        )
        conn.executemany(
            "INSERT INTO keywords(keyword, memory_id) VALUES (?, ?)",
            (("docker-compose", "legacy-alpha"), ("stable", "legacy-beta")),
        )
        if with_fts:
            conn.executemany(
                "INSERT INTO memories_fts(content, id) VALUES (?, ?)",
                ((row[1], row[0]) for row in LEGACY_ROWS),
            )


def _legacy_projection(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            """
            SELECT id, content, entities, timestamp, embedding, access_count,
                   session_id, tag, tier, last_accessed_at, last_demoted_at
            FROM memories ORDER BY id
            """
        ).fetchall()


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _insert_current_scoped_memory(
    conn: sqlite3.Connection,
    *,
    bootstrap: BootstrapIdentity,
    grant_id: str,
    memory_id: str,
    scope: str,
    content: str,
) -> None:
    conn.execute(
        """
        INSERT INTO memories (
            id, content, entities, timestamp, embedding, access_count,
            session_id, tag, tier, last_accessed_at, last_demoted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            content,
            "[]",
            bootstrap.created_at,
            None,
            0,
            "",
            "decision",
            "warm",
            None,
            None,
        ),
    )
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
            grant_id,
            1,
            scope,
            "decision",
            "[]",
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "kiro",
            None,
            "actor-current-scoped-search",
            bootstrap.created_at,
            bootstrap.created_at,
            None,
            0,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO memories_fts(content, id) VALUES (?, ?)",
        (content, memory_id),
    )


def test_upgrade_preserves_legacy_ids_content_and_count(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "legacy.db"
    backup_path = tmp_path / "backup.db"
    _create_legacy_db(db_path)
    before = _legacy_projection(db_path)

    report = migrate_database(
        db_path, backup_path=backup_path, bootstrap=bootstrap
    )

    assert report.from_version == 0
    assert report.to_version == 1
    assert report.applied_versions == (1,)
    assert _legacy_projection(db_path) == before

    with _readonly(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == len(
            LEGACY_ROWS
        )
        metadata = conn.execute(
            """
            SELECT memory_id, owner_id, creator_grant_id, revision, scope, kind,
                   content_hash, source_client, actor_id, reindex_required
            FROM memory_metadata ORDER BY memory_id
            """
        ).fetchall()
        assert metadata == [
            (
                row[0],
                bootstrap.owner_id,
                bootstrap.migration_grant_id,
                1,
                "legacy:unscoped",
                "legacy",
                hashlib.sha256(row[1].encode("utf-8")).hexdigest(),
                "legacy-import",
                "legacy-import",
                1 if row[4] is not None else 0,
            )
            for row in sorted(LEGACY_ROWS)
        ]
        assert conn.execute("SELECT COUNT(*) FROM embedding_profiles").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
        assert conn.execute(
            "SELECT id FROM memories_fts WHERE memories_fts MATCH 'docker'"
        ).fetchall() == [("legacy-alpha",)]


def test_legacy_rows_are_searchable_only_in_quarantine_scope(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "quarantine.db"
    _create_legacy_db(db_path)
    migrate_database(
        db_path,
        backup_path=tmp_path / "quarantine-backup.db",
        bootstrap=bootstrap,
    )
    current_grant_id = "grant-current-scoped-search"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO client_grants (
                grant_id, owner_id, issuer, subject, client_id,
                grant_generation, grant_kind, source_client,
                oauth_scopes_json, memory_scope_patterns_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_grant_id,
                bootstrap.owner_id,
                "https://issuer.invalid",
                "subject-current-scoped-search",
                "current-scoped-search",
                1,
                "OAUTH",
                "kiro",
                '["memory:read"]',
                '["global","project:recall"]',
                bootstrap.created_at,
            ),
        )
        _insert_current_scoped_memory(
            conn,
            bootstrap=bootstrap,
            grant_id=current_grant_id,
            memory_id="current-global",
            scope="global",
            content="Current global docker decision.",
        )
        _insert_current_scoped_memory(
            conn,
            bootstrap=bootstrap,
            grant_id=current_grant_id,
            memory_id="current-project",
            scope="project:recall",
            content="Current project stable decision.",
        )
    repository = RecallMCPRepository(db_path)

    quarantined = repository.search_scoped(
        grant_id=bootstrap.migration_grant_id,
        scope="legacy:unscoped",
        query="docker",
    )

    assert [result.memory_id for result in quarantined] == ["legacy-alpha"]
    assert quarantined[0].content == LEGACY_ROWS[0][1]
    assert quarantined[0].revision == 1
    assert quarantined[0].scope == "legacy:unscoped"
    assert quarantined[0].kind == "legacy"
    assert quarantined[0].content_hash == hashlib.sha256(
        LEGACY_ROWS[0][1].encode("utf-8")
    ).hexdigest()
    assert quarantined[0].source_client == "legacy-import"
    assert quarantined[0].actor_id == "legacy-import"
    assert quarantined[0].creator_grant_id == bootstrap.migration_grant_id
    assert quarantined[0].created_at == LEGACY_ROWS[0][3]
    assert quarantined[0].updated_at == LEGACY_ROWS[0][3]
    assert quarantined[0].source_conversation is None
    legacy_session = repository.search_scoped(
        grant_id=bootstrap.migration_grant_id,
        scope="legacy:unscoped",
        query="stable",
    )
    assert [result.memory_id for result in legacy_session] == ["legacy-beta"]
    assert legacy_session[0].source_conversation == "old-session"
    assert repository.search_scoped(
        grant_id="missing-grant",
        scope="legacy:unscoped",
        query="docker",
    ) == ()
    assert repository.search_scoped(
        grant_id=current_grant_id,
        scope="legacy:unscoped",
        query="docker",
    ) == ()
    global_results = repository.search_scoped(
        grant_id=current_grant_id,
        scope="global",
        query="docker",
    )
    assert [result.memory_id for result in global_results] == ["current-global"]
    project_results = repository.search_scoped(
        grant_id=current_grant_id,
        scope="project:recall",
        query="stable",
    )
    assert [result.memory_id for result in project_results] == ["current-project"]


def test_migration_rebuilds_missing_fts_for_quarantine_search(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "missing-fts.db"
    _create_legacy_db(db_path, with_fts=False)

    migrate_database(
        db_path,
        backup_path=tmp_path / "missing-fts-backup.db",
        bootstrap=bootstrap,
    )

    results = RecallMCPRepository(db_path).search_scoped(
        grant_id=bootstrap.migration_grant_id,
        scope="legacy:unscoped",
        query="docker",
    )
    assert [result.memory_id for result in results] == ["legacy-alpha"]


def test_migration_creates_required_schema_constraints_and_indexes(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "schema.db"
    _create_legacy_db(db_path)
    migrate_database(
        db_path, backup_path=tmp_path / "schema-backup.db", bootstrap=bootstrap
    )

    required_tables = {
        "schema_migrations",
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
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert required_tables <= tables

        indexes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        }
        assert "WHERE status = 'ACTIVE'" in indexes[
            "idx_embedding_profiles_one_active"
        ]
        assert "idx_memory_embeddings_owner_scope_gen" in indexes
        assert "idx_mcp_idempotency_memory" in indexes
        assert [
            row[2]
            for row in conn.execute(
                "PRAGMA index_info(idx_memory_embeddings_owner_scope_gen)"
            )
        ] == ["owner_id", "scope", "generation"]
        assert {
            row[1] for row in conn.execute("PRAGMA table_info(memory_events)")
        } >= {"owner_id", "grant_id"}
        assert {
            row[1] for row in conn.execute("PRAGMA table_info(mcp_idempotency)")
        } >= {
            "owner_id",
            "grant_id",
            "scope",
            "result_schema_version",
        }

        conn.execute(
            "INSERT INTO embedding_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bootstrap.owner_id,
                "global",
                1,
                "test-provider",
                "endpoint-digest",
                "test-model",
                2,
                "ACTIVE",
                bootstrap.created_at,
                bootstrap.created_at,
                None,
            ),
        )
        conn.execute(
            "INSERT INTO embedding_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bootstrap.owner_id,
                "global",
                2,
                "test-provider",
                "endpoint-digest",
                "test-model",
                2,
                "BUILDING",
                bootstrap.created_at,
                None,
                None,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO embedding_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bootstrap.owner_id,
                    "global",
                    3,
                    "test-provider",
                    "endpoint-digest",
                    "test-model",
                    2,
                    "ACTIVE",
                    bootstrap.created_at,
                    bootstrap.created_at,
                    None,
                ),
            )

        conn.execute(
            "INSERT INTO embedding_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bootstrap.owner_id,
                "legacy:unscoped",
                1,
                "test-provider",
                "endpoint-digest",
                "test-model",
                2,
                "BUILDING",
                bootstrap.created_at,
                None,
                None,
            ),
        )
        embedding_row = (
            "embedding-one",
            "legacy-alpha",
            bootstrap.owner_id,
            "legacy:unscoped",
            1,
            "test-provider",
            "endpoint-digest",
            "test-model",
            2,
            b"vector-one",
            None,
            bootstrap.created_at,
        )
        conn.execute(
            "INSERT INTO memory_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            embedding_row,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("embedding-two", *embedding_row[1:]),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO client_grants (
                    grant_id, owner_id, issuer, subject, client_id,
                    grant_generation, grant_kind, source_client,
                    oauth_scopes_json, memory_scope_patterns_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "orphan-grant",
                    "missing-owner",
                    "issuer",
                    "subject",
                    "client",
                    1,
                    "OAUTH",
                    "test",
                    "[]",
                    "[]",
                    bootstrap.created_at,
                ),
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_is_rerunnable_without_duplicate_bootstrap_or_metadata(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "rerun.db"
    _create_legacy_db(db_path)
    first = migrate_database(
        db_path, backup_path=tmp_path / "first-backup.db", bootstrap=bootstrap
    )
    second = migrate_database(
        db_path, backup_path=tmp_path / "second-backup.db", bootstrap=bootstrap
    )

    assert first.applied_versions == (1,)
    assert second.from_version == second.to_version == 1
    assert second.applied_versions == ()
    with _readonly(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM client_grants").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_metadata").fetchone()[0] == len(
            LEGACY_ROWS
        )


def test_migration_rerun_accepts_valid_post_migration_memory(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "rerun-with-current-data.db"
    _create_legacy_db(db_path)
    migrate_database(
        db_path, backup_path=tmp_path / "initial-backup.db", bootstrap=bootstrap
    )

    content = "A valid MCP-era memory must not make db migrate fail."
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO client_grants (
                grant_id, owner_id, issuer, subject, client_id,
                grant_generation, grant_kind, source_client,
                oauth_scopes_json, memory_scope_patterns_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "grant-current-client",
                bootstrap.owner_id,
                "https://issuer.invalid",
                "subject-current",
                "current-client",
                1,
                "OAUTH",
                "kiro",
                '["memory:read","memory:write"]',
                '["global"]',
                bootstrap.created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO memories (
                id, content, entities, timestamp, embedding, access_count,
                session_id, tag, tier, last_accessed_at, last_demoted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "current-memory",
                content,
                "[]",
                bootstrap.created_at,
                None,
                0,
                "",
                "decision",
                "warm",
                None,
                None,
            ),
        )
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
                "current-memory",
                bootstrap.owner_id,
                "grant-current-client",
                1,
                "global",
                "decision",
                "[]",
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "kiro",
                None,
                "actor-current",
                bootstrap.created_at,
                bootstrap.created_at,
                None,
                0,
                None,
            ),
        )

    report = migrate_database(
        db_path,
        backup_path=tmp_path / "rerun-current-backup.db",
        bootstrap=bootstrap,
    )
    assert report.applied_versions == ()
    with _readonly(db_path) as conn:
        assert conn.execute(
            "SELECT content FROM memories WHERE id='current-memory'"
        ).fetchone() == (content,)


def test_rerun_rejects_same_name_wrong_covering_index(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "index-drift.db"
    _create_legacy_db(db_path)
    migrate_database(
        db_path, backup_path=tmp_path / "index-initial-backup.db", bootstrap=bootstrap
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_memory_embeddings_owner_scope_gen")
        conn.execute(
            """
            CREATE INDEX idx_memory_embeddings_owner_scope_gen
            ON memory_embeddings(memory_id)
            """
        )

    with pytest.raises(MigrationVerificationError, match="index"):
        migrate_database(
            db_path,
            backup_path=tmp_path / "index-drift-backup.db",
            bootstrap=bootstrap,
        )


def test_injected_migration_failure_rolls_back_schema_data_and_version(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "rollback.db"
    backup_path = tmp_path / "rollback-backup.db"
    _create_legacy_db(db_path)
    before = _legacy_projection(db_path)

    def fail_after_ddl(conn: sqlite3.Connection, _: BootstrapIdentity) -> None:
        conn.execute("CREATE TABLE must_rollback(value TEXT)")
        conn.execute("INSERT INTO must_rollback VALUES ('partial')")
        raise RuntimeError("deterministic migration failure")

    with pytest.raises(RuntimeError, match="deterministic migration failure"):
        migrate_database(
            db_path,
            backup_path=backup_path,
            bootstrap=bootstrap,
            migrations=(Migration(1, "intentional_failure", fail_after_ddl),),
        )

    assert backup_path.exists()
    assert _legacy_projection(db_path) == before
    with _readonly(db_path) as conn:
        objects = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert "must_rollback" not in objects
        assert "schema_migrations" not in objects


def test_backup_is_pre_migration_snapshot_and_reopen_readback_passes(
    tmp_path: Path, bootstrap: BootstrapIdentity
) -> None:
    db_path = tmp_path / "readback.db"
    backup_path = tmp_path / "readback-backup.db"
    _create_legacy_db(db_path)
    before = _legacy_projection(db_path)

    migrate_database(db_path, backup_path=backup_path, bootstrap=bootstrap)

    with _readonly(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
        assert backup.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None
        assert backup.execute(
            "SELECT id, content FROM memories ORDER BY id"
        ).fetchall() == [(row[0], row[1]) for row in before]

    with _readonly(db_path) as migrated:
        assert migrated.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.execute(
            "SELECT version, name FROM schema_migrations"
        ).fetchall() == [(1, "mcp_shared_memory_schema")]
        assert migrated.execute("PRAGMA user_version").fetchone() == (1,)
