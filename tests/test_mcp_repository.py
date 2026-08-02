"""Transactional repository tests for the shared Recall MCP authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recall.mcp_repository import (
    PurgedMemoryReplayError,
    RecallMCPRepository,
    RevisionConflictError,
)
from recall.migrations import BootstrapIdentity, migrate_database

BOOTSTRAP = BootstrapIdentity(
    owner_id="owner-repository-test",
    migration_grant_id="grant-repository-migration",
    created_at="2026-08-02T04:00:00+00:00",
)
WRITE_GRANT_ID = "grant-repository-writer"
ADMIN_GRANT_ID = "grant-repository-admin"
MEMORY_ID = "memory-transaction-rollback"


def _create_empty_legacy_db(path: Path) -> None:
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
        conn.execute(
            """
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                content, id UNINDEXED, tokenize='porter unicode61'
            )
            """
        )


def _create_migrated_authority(path: Path, backup_path: Path) -> None:
    _create_empty_legacy_db(path)
    migrate_database(path, backup_path=backup_path, bootstrap=BOOTSTRAP)
    with sqlite3.connect(path) as conn:
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
                WRITE_GRANT_ID,
                BOOTSTRAP.owner_id,
                "https://issuer.invalid",
                "repository-writer",
                "kiro-test",
                1,
                "OAUTH",
                "kiro",
                '["memory:read","memory:write"]',
                '["project:recall"]',
                BOOTSTRAP.created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO client_grants (
                grant_id, owner_id, issuer, subject, client_id,
                grant_generation, grant_kind, source_client,
                oauth_scopes_json, memory_scope_patterns_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ADMIN_GRANT_ID,
                BOOTSTRAP.owner_id,
                "https://issuer.invalid",
                "repository-admin",
                "admin-cli-test",
                1,
                "OAUTH",
                "admin-cli",
                '["memory:read","memory:write","memory:admin"]',
                '["project:recall"]',
                BOOTSTRAP.created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                owner_id, scope, generation, provider,
                endpoint_identity_hash, model, dimension, status,
                created_at, activated_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP.owner_id,
                "project:recall",
                1,
                "test-provider",
                "endpoint-identity-digest",
                "test-embedding-model",
                2,
                "ACTIVE",
                BOOTSTRAP.created_at,
                BOOTSTRAP.created_at,
                None,
            ),
        )


class _FailAfterIdempotencyRepository(RecallMCPRepository):
    def _after_write_stage(self, stage: str, conn: sqlite3.Connection) -> None:
        super()._after_write_stage(stage, conn)
        if stage == "idempotency":
            raise RuntimeError("deterministic failure after all derived writes")


def _table_count_for_memory(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE memory_id = ?', (MEMORY_ID,)
        ).fetchone()[0]
    )


def test_add_rolls_back_primary_and_all_derived_rows_when_final_stage_fails(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority.db"
    _create_migrated_authority(db_path, tmp_path / "authority-backup.db")
    repository = _FailAfterIdempotencyRepository(db_path)

    with pytest.raises(
        RuntimeError, match="deterministic failure after all derived writes"
    ):
        repository.add_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            content="Transactional rollback keeps SQLite indexes in parity.",
            kind="decision",
            tags=("sqlite", "mcp"),
            source_conversation="conversation-test",
            actor_id="actor-repository-writer",
            idempotency_key="idempotency-add-rollback",
            payload_digest="keyed-payload-digest",
            occurred_at="2026-08-02T04:05:00+00:00",
            embedding_blobs={1: b"two-float-vector"},
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id = ?", (MEMORY_ID,)
        ).fetchone() == (0,)
        assert _table_count_for_memory(conn, "keywords") == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE id = ?", (MEMORY_ID,)
        ).fetchone() == (0,)
        assert _table_count_for_memory(conn, "memory_metadata") == 0
        assert _table_count_for_memory(conn, "memory_events") == 0
        assert _table_count_for_memory(conn, "memory_embeddings") == 0
        assert _table_count_for_memory(conn, "mcp_idempotency") == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_add_commits_primary_indexes_metadata_event_embedding_and_idempotency(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-success.db"
    _create_migrated_authority(db_path, tmp_path / "authority-success-backup.db")
    repository = RecallMCPRepository(db_path)

    result = repository.add_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        content="Transactional add keeps every SQLite index in parity.",
        kind="decision",
        tags=("sqlite", "mcp"),
        source_conversation="conversation-success",
        actor_id="actor-repository-writer",
        idempotency_key="idempotency-add-success",
        payload_digest="keyed-payload-digest-success",
        occurred_at="2026-08-02T04:06:00+00:00",
        embedding_blobs={1: b"two-float-vector"},
    )

    assert result.memory_id == MEMORY_ID
    assert result.revision == 1
    assert result.created_at == "2026-08-02T04:06:00+00:00"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT content, session_id, tag FROM memories WHERE id = ?", (MEMORY_ID,)
        ).fetchone() == (
            "Transactional add keeps every SQLite index in parity.",
            "conversation-success",
            "decision",
        )
        assert conn.execute(
            "SELECT keyword FROM keywords WHERE memory_id = ? ORDER BY keyword",
            (MEMORY_ID,),
        ).fetchall()
        assert conn.execute(
            "SELECT content FROM memories_fts WHERE id = ?", (MEMORY_ID,)
        ).fetchone() == ("Transactional add keeps every SQLite index in parity.",)
        assert conn.execute(
            """
            SELECT owner_id, creator_grant_id, revision, scope, kind, tags_json,
                   source_client, source_conversation, actor_id, deleted_at,
                   reindex_required
            FROM memory_metadata WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchone() == (
            BOOTSTRAP.owner_id,
            WRITE_GRANT_ID,
            1,
            "project:recall",
            "decision",
            '["sqlite","mcp"]',
            "kiro",
            "conversation-success",
            "actor-repository-writer",
            None,
            0,
        )
        assert conn.execute(
            """
            SELECT generation, provider, model, dimension, embedding_blob
            FROM memory_embeddings WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, "test-provider", "test-embedding-model", 2, b"two-float-vector")
        ]
        assert conn.execute(
            """
            SELECT revision, operation, actor_id, source_client, payload_digest
            FROM memory_events WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchone() == (
            1,
            "add",
            "actor-repository-writer",
            "kiro",
            "keyed-payload-digest-success",
        )
        idempotency = conn.execute(
            """
            SELECT owner_id, grant_id, operation, scope, result_schema_version,
                   payload_digest, result_json, purged_at
            FROM mcp_idempotency WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchone()
        assert idempotency[:6] == (
            BOOTSTRAP.owner_id,
            WRITE_GRANT_ID,
            "add",
            "project:recall",
            1,
            "keyed-payload-digest-success",
        )
        assert '"memory_id":"memory-transaction-rollback"' in idempotency[6]
        assert idempotency[7] is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    results = repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Transactional",
    )
    assert [row.memory_id for row in results] == [MEMORY_ID]


def test_add_dual_writes_active_and_building_without_changing_active_search(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-add-dual-write.db"
    _create_migrated_authority(
        db_path, tmp_path / "authority-add-dual-write-backup.db"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                owner_id, scope, generation, provider,
                endpoint_identity_hash, model, dimension, status,
                created_at, activated_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP.owner_id,
                "project:recall",
                2,
                "test-provider-v2",
                "endpoint-identity-digest-v2",
                "test-embedding-model-v2",
                2,
                "BUILDING",
                "2026-08-02T04:06:30+00:00",
                None,
                None,
            ),
        )
    repository = RecallMCPRepository(db_path)

    repository.add_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        content="Active search remains stable during a building dual-write.",
        kind="decision",
        tags=("dual-write",),
        source_conversation="conversation-dual-write",
        actor_id="actor-repository-writer",
        idempotency_key="idempotency-add-dual-write",
        payload_digest="keyed-payload-digest-add-dual-write",
        occurred_at="2026-08-02T04:06:31+00:00",
        embedding_blobs={
            1: b"active-generation-vector",
            2: b"building-generation-vector",
        },
    )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """
            SELECT me.generation, ep.status, me.embedding_blob
            FROM memory_embeddings AS me
            JOIN embedding_profiles AS ep
              ON ep.owner_id = me.owner_id
             AND ep.scope = me.scope
             AND ep.generation = me.generation
            WHERE me.memory_id = ?
            ORDER BY me.generation
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, "ACTIVE", b"active-generation-vector"),
            (2, "BUILDING", b"building-generation-vector"),
        ]
        assert conn.execute(
            """
            SELECT generation FROM embedding_profiles
            WHERE owner_id = ? AND scope = ? AND status = 'ACTIVE'
            """,
            (BOOTSTRAP.owner_id, "project:recall"),
        ).fetchall() == [(1,)]
    results = repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Active",
    )
    assert [row.memory_id for row in results] == [MEMORY_ID]


def _add_building_generation(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                owner_id, scope, generation, provider,
                endpoint_identity_hash, model, dimension, status,
                created_at, activated_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP.owner_id,
                "project:recall",
                2,
                "test-provider",
                "endpoint-identity-digest-v2",
                "test-embedding-model-v2",
                2,
                "BUILDING",
                "2026-08-02T04:07:00+00:00",
                None,
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO memory_embeddings (
                embedding_id, memory_id, owner_id, scope, generation,
                provider, endpoint_identity_hash, model, dimension,
                embedding_blob, vector_rowid, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "embedding-building-before-replace",
                MEMORY_ID,
                BOOTSTRAP.owner_id,
                "project:recall",
                2,
                "test-provider",
                "endpoint-identity-digest-v2",
                "test-embedding-model-v2",
                2,
                b"old-building-vector",
                None,
                "2026-08-02T04:07:00+00:00",
            ),
        )


def _seed_replace_target(path: Path, backup_path: Path) -> RecallMCPRepository:
    _create_migrated_authority(path, backup_path)
    repository = RecallMCPRepository(path)
    repository.add_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        content="Original transactional content must disappear from every index.",
        kind="decision",
        tags=("original",),
        source_conversation="conversation-original",
        actor_id="actor-original",
        idempotency_key="idempotency-add-before-replace",
        payload_digest="keyed-payload-digest-add-before-replace",
        occurred_at="2026-08-02T04:06:00+00:00",
        embedding_blobs={1: b"old-active-vector"},
    )
    _add_building_generation(path)
    return repository


def _snapshot_memory_state(path: Path) -> dict[str, list[tuple[object, ...]]]:
    queries = {
        "memories": "SELECT * FROM memories WHERE id = ?",
        "keywords": (
            "SELECT * FROM keywords WHERE memory_id = ? ORDER BY keyword, memory_id"
        ),
        "fts": "SELECT rowid, content, id FROM memories_fts WHERE id = ? ORDER BY rowid",
        "metadata": "SELECT * FROM memory_metadata WHERE memory_id = ?",
        "embeddings": (
            "SELECT * FROM memory_embeddings WHERE memory_id = ? ORDER BY generation"
        ),
        "events": (
            "SELECT * FROM memory_events WHERE memory_id = ? ORDER BY revision, event_id"
        ),
        "idempotency": (
            "SELECT * FROM mcp_idempotency WHERE memory_id = ? "
            "ORDER BY operation, idempotency_key"
        ),
    }
    with sqlite3.connect(path) as conn:
        return {
            name: conn.execute(sql, (MEMORY_ID,)).fetchall()
            for name, sql in queries.items()
        }


def _attach_generation_vector_tables(path: Path) -> dict[int, str]:
    import sqlite_vec

    table_names: dict[int, str] = {}
    with sqlite3.connect(path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for generation in (1, 2):
            table_name = f"vec_reindex_generation_{generation}"
            conn.execute(
                f'CREATE VIRTUAL TABLE "{table_name}" '
                "USING vec0(embedding float[2] distance_metric=cosine)"
            )
            cursor = conn.execute(
                f'INSERT INTO "{table_name}"(embedding) VALUES (?)',
                (struct.pack("<2f", float(generation), float(generation + 1)),),
            )
            conn.execute(
                """
                INSERT INTO vector_tables (
                    owner_id, scope, generation, table_name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    BOOTSTRAP.owner_id,
                    "project:recall",
                    generation,
                    table_name,
                    "2026-08-02T04:07:30+00:00",
                ),
            )
            conn.execute(
                """
                UPDATE memory_embeddings SET vector_rowid = ?
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND generation = ?
                """,
                (
                    cursor.lastrowid,
                    MEMORY_ID,
                    BOOTSTRAP.owner_id,
                    "project:recall",
                    generation,
                ),
            )
            table_names[generation] = table_name
    return table_names


def test_failed_building_generation_cleanup_preserves_active_generation(
    tmp_path: Path,
) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-failed-building-cleanup.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-failed-building-cleanup-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    before_memory = _snapshot_memory_state(db_path)

    repository.fail_embedding_generation(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        generation=2,
        failed_at="2026-08-02T04:08:00+00:00",
    )

    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        assert conn.execute(
            """
            SELECT generation, status, activated_at, retired_at
            FROM embedding_profiles
            WHERE owner_id = ? AND scope = ?
            ORDER BY generation
            """,
            (BOOTSTRAP.owner_id, "project:recall"),
        ).fetchall() == [
            (1, "ACTIVE", BOOTSTRAP.created_at, None),
            (2, "FAILED", None, "2026-08-02T04:08:00+00:00"),
        ]
        assert conn.execute(
            """
            SELECT generation FROM memory_embeddings
            WHERE memory_id = ? ORDER BY generation
            """,
            (MEMORY_ID,),
        ).fetchall() == [(1,)]
        assert conn.execute(
            """
            SELECT generation, table_name FROM vector_tables
            WHERE owner_id = ? AND scope = ? ORDER BY generation
            """,
            (BOOTSTRAP.owner_id, "project:recall"),
        ).fetchall() == [(1, table_names[1])]
        assert conn.execute(
            f'SELECT COUNT(*) FROM "{table_names[1]}"'
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_names[2],),
        ).fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    after_memory = _snapshot_memory_state(db_path)
    for key in ("memories", "keywords", "fts", "metadata", "events", "idempotency"):
        assert after_memory[key] == before_memory[key]
    assert [row[4] for row in after_memory["embeddings"]] == [1]


def test_successful_embedding_generation_cutover_switches_only_profile_status(
    tmp_path: Path,
) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-generation-cutover.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-generation-cutover-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    before_memory = _snapshot_memory_state(db_path)

    repository.cutover_embedding_generation(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        expected_active_generation=1,
        building_generation=2,
        activated_at="2026-08-02T04:09:00+00:00",
    )

    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        assert conn.execute(
            """
            SELECT generation, status, activated_at, retired_at
            FROM embedding_profiles
            WHERE owner_id = ? AND scope = ? ORDER BY generation
            """,
            (BOOTSTRAP.owner_id, "project:recall"),
        ).fetchall() == [
            (1, "RETIRED", BOOTSTRAP.created_at, "2026-08-02T04:09:00+00:00"),
            (2, "ACTIVE", "2026-08-02T04:09:00+00:00", None),
        ]
        assert conn.execute(
            """
            SELECT generation, table_name FROM vector_tables
            WHERE owner_id = ? AND scope = ? ORDER BY generation
            """,
            (BOOTSTRAP.owner_id, "project:recall"),
        ).fetchall() == [(1, table_names[1]), (2, table_names[2])]
        for table_name in table_names.values():
            assert conn.execute(
                f'SELECT COUNT(*) FROM "{table_name}"'
            ).fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _snapshot_memory_state(db_path) == before_memory


def _fail_at_stage(target_stage: str) -> type[RecallMCPRepository]:
    class _FailAtStageRepository(RecallMCPRepository):
        def _after_write_stage(self, stage: str, conn: sqlite3.Connection) -> None:
            super()._after_write_stage(stage, conn)
            if stage == target_stage:
                raise RuntimeError(f"deterministic failure at {target_stage}")

    return _FailAtStageRepository


def _snapshot_generation_state(
    path: Path, table_names: dict[int, str]
) -> dict[str, list[tuple[object, ...]]]:
    import sqlite_vec

    with sqlite3.connect(path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        state: dict[str, list[tuple[object, ...]]] = {
            "profiles": conn.execute(
                """
                SELECT owner_id, scope, generation, status, activated_at, retired_at
                FROM embedding_profiles ORDER BY owner_id, scope, generation
                """
            ).fetchall(),
            "registry": conn.execute(
                """
                SELECT owner_id, scope, generation, table_name
                FROM vector_tables ORDER BY owner_id, scope, generation
                """
            ).fetchall(),
            "mappings": conn.execute(
                """
                SELECT memory_id, owner_id, scope, generation, vector_rowid
                FROM memory_embeddings ORDER BY memory_id, generation
                """
            ).fetchall(),
            "tables": conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall(),
        }
        for generation, table_name in sorted(table_names.items()):
            state[f"vectors-{generation}"] = conn.execute(
                f'SELECT rowid FROM "{table_name}" ORDER BY rowid'
            ).fetchall()
        return state


def test_cutover_rejects_stale_expected_active_generation_without_side_effects(
    tmp_path: Path,
) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-cutover-stale.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-cutover-stale-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    before = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(PermissionError, match="not authorized"):
        repository.cutover_embedding_generation(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            expected_active_generation=7,
            building_generation=2,
            activated_at="2026-08-02T04:09:00+00:00",
        )

    assert _snapshot_generation_state(db_path, table_names) == before


def test_cutover_requires_owner_admin_without_side_effects(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-cutover-auth.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-cutover-auth-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    before = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(PermissionError, match="not authorized"):
        repository.cutover_embedding_generation(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            expected_active_generation=1,
            building_generation=2,
            activated_at="2026-08-02T04:09:00+00:00",
        )

    assert _snapshot_generation_state(db_path, table_names) == before


def test_cutover_rolls_back_when_activation_stage_fails(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-cutover-rollback.db"
    _seed_replace_target(db_path, tmp_path / "authority-cutover-rollback-backup.db")
    table_names = _attach_generation_vector_tables(db_path)
    before = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(
        RuntimeError, match="deterministic failure at cutover_activated_building"
    ):
        _fail_at_stage("cutover_activated_building")(db_path).cutover_embedding_generation(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            expected_active_generation=1,
            building_generation=2,
            activated_at="2026-08-02T04:09:00+00:00",
        )

    assert _snapshot_generation_state(db_path, table_names) == before


def test_cutover_fails_closed_when_building_generation_lacks_parity(
    tmp_path: Path,
) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-cutover-parity.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-cutover-parity-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            DELETE FROM memory_embeddings
            WHERE memory_id = ? AND generation = 2
            """,
            (MEMORY_ID,),
        )
    before = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(RuntimeError, match="embedding generation parity mismatch"):
        repository.cutover_embedding_generation(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            expected_active_generation=1,
            building_generation=2,
            activated_at="2026-08-02T04:09:00+00:00",
        )

    assert _snapshot_generation_state(db_path, table_names) == before


def test_failed_building_cleanup_requires_owner_admin_without_side_effects(
    tmp_path: Path,
) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-failed-building-auth.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-failed-building-auth-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    before = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(PermissionError, match="not authorized"):
        repository.fail_embedding_generation(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            generation=2,
            failed_at="2026-08-02T04:08:00+00:00",
        )
    with pytest.raises(PermissionError, match="not authorized"):
        repository.fail_embedding_generation(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            generation=1,
            failed_at="2026-08-02T04:08:00+00:00",
        )

    assert _snapshot_generation_state(db_path, table_names) == before


def test_failed_building_cleanup_rolls_back_dropped_table_on_final_failure(
    tmp_path: Path,
) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-failed-building-rollback.db"
    _seed_replace_target(
        db_path, tmp_path / "authority-failed-building-rollback-backup.db"
    )
    table_names = _attach_generation_vector_tables(db_path)
    before = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(
        RuntimeError, match="deterministic failure at failed_generation_profile"
    ):
        _fail_at_stage("failed_generation_profile")(db_path).fail_embedding_generation(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            generation=2,
            failed_at="2026-08-02T04:08:00+00:00",
        )

    assert _snapshot_generation_state(db_path, table_names) == before


def test_replace_rebuilds_all_indexes_and_increments_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-replace.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-replace-backup.db"
    )

    result = repository.replace_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=1,
        content="Replaced canonical content reaches active and building indexes.",
        kind="semantic",
        tags=("replaced", "sqlite"),
        source_conversation="conversation-replaced",
        actor_id="actor-replacer",
        idempotency_key="idempotency-replace-success",
        payload_digest="keyed-payload-digest-replace-success",
        occurred_at="2026-08-02T04:08:00+00:00",
        embedding_blobs={1: b"new-active-vector", 2: b"new-building-vector"},
    )

    assert result.memory_id == MEMORY_ID
    assert result.revision == 2
    assert result.updated_at == "2026-08-02T04:08:00+00:00"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT content, session_id, tag FROM memories WHERE id = ?", (MEMORY_ID,)
        ).fetchone() == (
            "Replaced canonical content reaches active and building indexes.",
            "conversation-replaced",
            "semantic",
        )
        keywords = {
            row[0]
            for row in conn.execute(
                "SELECT keyword FROM keywords WHERE memory_id = ?", (MEMORY_ID,)
            )
        }
        assert "replaced" in keywords
        assert "original" not in keywords
        assert conn.execute(
            "SELECT content FROM memories_fts WHERE id = ?", (MEMORY_ID,)
        ).fetchall() == [
            ("Replaced canonical content reaches active and building indexes.",)
        ]
        assert conn.execute(
            """
            SELECT creator_grant_id, revision, kind, tags_json, content_hash,
                   source_conversation, actor_id, created_at, updated_at, deleted_at
            FROM memory_metadata WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchone() == (
            WRITE_GRANT_ID,
            2,
            "semantic",
            '["replaced","sqlite"]',
            hashlib.sha256(
                b"Replaced canonical content reaches active and building indexes."
            ).hexdigest(),
            "conversation-replaced",
            "actor-replacer",
            "2026-08-02T04:06:00+00:00",
            "2026-08-02T04:08:00+00:00",
            None,
        )
        assert conn.execute(
            """
            SELECT generation, embedding_blob FROM memory_embeddings
            WHERE memory_id = ? ORDER BY generation
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, b"new-active-vector"),
            (2, b"new-building-vector"),
        ]
        assert conn.execute(
            """
            SELECT revision, operation, actor_id, payload_digest
            FROM memory_events WHERE memory_id = ? ORDER BY revision
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, "add", "actor-original", "keyed-payload-digest-add-before-replace"),
            (2, "replace", "actor-replacer", "keyed-payload-digest-replace-success"),
        ]
        idempotency_rows = conn.execute(
            """
            SELECT operation, idempotency_key, result_json
            FROM mcp_idempotency WHERE memory_id = ? ORDER BY operation
            """,
            (MEMORY_ID,),
        ).fetchall()
        assert idempotency_rows[1][:2] == (
            "replace",
            "idempotency-replace-success",
        )
        assert json.loads(idempotency_rows[1][2]) == {
            "memory_id": MEMORY_ID,
            "revision": 2,
            "updated_at": "2026-08-02T04:08:00+00:00",
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Original",
    ) == ()
    replaced = repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Replaced",
    )
    assert [(row.memory_id, row.revision) for row in replaced] == [(MEMORY_ID, 2)]


def test_replace_scope_move_atomically_updates_metadata_and_all_generations(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-replace-scope-move.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-replace-scope-move-backup.db"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            UPDATE client_grants
            SET memory_scope_patterns_json = ?
            WHERE grant_id = ?
            """,
            ('["project:recall","project:archive"]', WRITE_GRANT_ID),
        )
        source_profiles = conn.execute(
            """
            SELECT generation, provider, endpoint_identity_hash, model,
                   dimension, status, created_at, activated_at, retired_at
            FROM embedding_profiles
            WHERE owner_id = ? AND scope = ? ORDER BY generation
            """,
            (BOOTSTRAP.owner_id, "project:recall"),
        ).fetchall()
        for profile in source_profiles:
            conn.execute(
                """
                INSERT INTO embedding_profiles (
                    owner_id, scope, generation, provider,
                    endpoint_identity_hash, model, dimension, status,
                    created_at, activated_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (BOOTSTRAP.owner_id, "project:archive", *profile),
            )

    result = repository.replace_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        new_scope="project:archive",
        memory_id=MEMORY_ID,
        expected_revision=1,
        content="Scope move remains atomic across every embedding generation.",
        kind="decision",
        tags=("scope-move",),
        source_conversation="conversation-scope-move",
        actor_id="actor-scope-move",
        idempotency_key="idempotency-replace-scope-move",
        payload_digest="keyed-payload-digest-replace-scope-move",
        occurred_at="2026-08-02T04:08:30+00:00",
        embedding_blobs={1: b"archive-active", 2: b"archive-building"},
    )

    assert result.revision == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT scope, revision FROM memory_metadata WHERE memory_id = ?",
            (MEMORY_ID,),
        ).fetchone() == ("project:archive", 2)
        assert conn.execute(
            """
            SELECT generation, scope, embedding_blob FROM memory_embeddings
            WHERE memory_id = ? ORDER BY generation
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, "project:archive", b"archive-active"),
            (2, "project:archive", b"archive-building"),
        ]
        assert conn.execute(
            """
            SELECT scope FROM mcp_idempotency
            WHERE memory_id = ? AND operation = 'replace'
            """,
            (MEMORY_ID,),
        ).fetchone() == ("project:archive",)
        assert conn.execute(
            """
            SELECT revision, operation FROM memory_events
            WHERE memory_id = ? ORDER BY revision
            """,
            (MEMORY_ID,),
        ).fetchall() == [(1, "add"), (2, "replace")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Scope",
    ) == ()
    moved = repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:archive",
        query="Scope",
    )
    assert [(row.memory_id, row.revision) for row in moved] == [(MEMORY_ID, 2)]


def test_purged_memory_replay_raises_purged_memory_replay_error(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-purged-replay.db"
    _create_migrated_authority(
        db_path, tmp_path / "authority-purged-replay-backup.db"
    )
    repository = RecallMCPRepository(db_path)
    repository.add_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        content="Original content predates purge",
        kind="decision",
        tags=("original",),
        source_conversation="conv-original",
        actor_id="actor-original",
        idempotency_key="idempotency-add-before-purge",
        payload_digest="digest-add-before-purge",
        occurred_at="2026-08-02T04:14:00+00:00",
        embedding_blobs={1: b"vec"},
    )

    # Hard-purge memory
    repository.purge_memory(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=1,
        actor_id="actor-purger",
        idempotency_key="idempotency-purge-key",
        payload_digest="keyed-payload-digest-purge",
        deny_payload_digest="keyed-payload-digest-deny",
        occurred_at="2026-08-02T04:15:00+00:00",
    )

    # Re-check with the same idempotency key used during purge ("idempotency-add-before-purge")
    with pytest.raises(PurgedMemoryReplayError) as exc_add:
        repository.add_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id="new-memory-purged-key-replay",
            content="Attempting add on purged key",
            kind="decision",
            tags=("test",),
            source_conversation="conv-test",
            actor_id="actor-test",
            idempotency_key="idempotency-add-before-purge",
            payload_digest="keyed-payload-digest-add",
            occurred_at="2026-08-02T04:16:00+00:00",
            embedding_blobs={1: b"vec"},
        )
    assert exc_add.value.memory_id == MEMORY_ID

    with pytest.raises(PurgedMemoryReplayError) as exc_replace:
        repository.replace_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=1,
            content="Attempting replace on purged key",
            kind="decision",
            tags=("test",),
            source_conversation="conv-test",
            actor_id="actor-test",
            idempotency_key="idempotency-add-before-purge",
            payload_digest="keyed-payload-digest-replace",
            occurred_at="2026-08-02T04:16:00+00:00",
            embedding_blobs={1: b"vec1", 2: b"vec2"},
        )
    assert exc_replace.value.memory_id == MEMORY_ID

    with pytest.raises(PurgedMemoryReplayError) as exc_remove:
        repository.remove_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=1,
            actor_id="actor-test",
            idempotency_key="idempotency-add-before-purge",
            payload_digest="keyed-payload-digest-remove",
            occurred_at="2026-08-02T04:16:00+00:00",
        )
    assert exc_remove.value.memory_id == MEMORY_ID


def _row_counts_for_memory(path: Path, memory_id: str) -> dict[str, int]:
    """Count every derived/audit row that belongs to one memory id."""

    queries = {
        "memories": "SELECT COUNT(*) FROM memories WHERE id = ?",
        "keywords": "SELECT COUNT(*) FROM keywords WHERE memory_id = ?",
        "fts": "SELECT COUNT(*) FROM memories_fts WHERE id = ?",
        "metadata": "SELECT COUNT(*) FROM memory_metadata WHERE memory_id = ?",
        "embeddings": "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
        "events": "SELECT COUNT(*) FROM memory_events WHERE memory_id = ?",
        "idempotency": "SELECT COUNT(*) FROM mcp_idempotency WHERE memory_id = ?",
    }
    with sqlite3.connect(path) as conn:
        return {
            name: int(conn.execute(sql, (memory_id,)).fetchone()[0])
            for name, sql in queries.items()
        }


def test_transactional_rollback_on_write_stage_failures(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-stage-rollback.db"
    _seed_replace_target(db_path, tmp_path / "authority-stage-rollback-backup.db")
    before_memory = _snapshot_memory_state(db_path)
    new_memory_id = "new-memory-rollback-test"
    empty_counts = dict.fromkeys(
        (
            "memories",
            "keywords",
            "fts",
            "metadata",
            "embeddings",
            "events",
            "idempotency",
        ),
        0,
    )
    assert _row_counts_for_memory(db_path, new_memory_id) == empty_counts

    # Test failure at each stage of add_memory
    for stage in ("memory", "keywords", "fts", "metadata", "embeddings", "event", "idempotency"):
        repo = _fail_at_stage(stage)(db_path)
        with pytest.raises(RuntimeError, match=f"deterministic failure at {stage}"):
            repo.add_memory(
                grant_id=WRITE_GRANT_ID,
                scope="project:recall",
                memory_id=new_memory_id,
                content="Content that will fail at stage",
                kind="decision",
                tags=("fail",),
                source_conversation="conv-fail",
                actor_id="actor-fail",
                idempotency_key=f"idempotency-fail-{stage}",
                payload_digest=f"digest-fail-{stage}",
                occurred_at="2026-08-02T04:17:00+00:00",
                embedding_blobs={1: b"vec-active", 2: b"vec-building"},
            )
        # The failed add leaves no partial row of its own ...
        assert _row_counts_for_memory(db_path, new_memory_id) == empty_counts
        # ... and does not disturb the already-committed memory.
        assert _snapshot_memory_state(db_path) == before_memory
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_tombstone_safe_read_path_and_pure_search_and_get(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-read-path.db"
    _seed_replace_target(db_path, tmp_path / "authority-read-path-backup.db")
    repository = RecallMCPRepository(db_path)

    # 1. Active memory read-back
    mem_get = repository.get_scoped_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
    )
    assert mem_get is not None
    assert mem_get.memory_id == MEMORY_ID
    assert mem_get.revision == 1

    recent = repository.recent_scoped_memories(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
    )
    assert [m.memory_id for m in recent] == [MEMORY_ID]

    # 2. Soft remove
    repository.remove_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=1,
        actor_id="actor-remover",
        idempotency_key="idempotency-remove-read-test",
        payload_digest="keyed-payload-digest-remove-read-test",
        occurred_at="2026-08-02T04:11:00+00:00",
    )

    # 3. Verify tombstone-safe exclusion in get, search, and recent
    assert (
        repository.get_scoped_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
        )
        is None
    )
    assert (
        repository.search_scoped(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            query="Original",
        )
        == ()
    )
    assert (
        repository.recent_scoped_memories(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
        )
        == ()
    )

    # Pure read path verification after reads (DB state remains identical to post-remove state)
    after_remove_snapshot = _snapshot_memory_state(db_path)
    # Perform reads again
    _ = repository.get_scoped_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
    )
    _ = repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Original",
    )
    _ = repository.recent_scoped_memories(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
    )
    assert _snapshot_memory_state(db_path) == after_remove_snapshot


def test_replace_backfills_building_generation_added_after_the_memory(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-replace-building-backfill.db"
    _create_migrated_authority(
        db_path, tmp_path / "authority-replace-building-backfill-backup.db"
    )
    repository = RecallMCPRepository(db_path)
    repository.add_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        content="Original content predates the building generation.",
        kind="decision",
        tags=("original",),
        source_conversation="conversation-original",
        actor_id="actor-original",
        idempotency_key="idempotency-add-before-building",
        payload_digest="keyed-payload-digest-add-before-building",
        occurred_at="2026-08-02T04:06:00+00:00",
        embedding_blobs={1: b"old-active-vector"},
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                owner_id, scope, generation, provider,
                endpoint_identity_hash, model, dimension, status,
                created_at, activated_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP.owner_id,
                "project:recall",
                2,
                "test-provider",
                "endpoint-identity-digest-v2",
                "test-embedding-model-v2",
                2,
                "BUILDING",
                "2026-08-02T04:07:00+00:00",
                None,
                None,
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
            (MEMORY_ID,),
        ).fetchone() == (1,)

    result = repository.replace_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=1,
        content="Replacement during reindex must reach the building generation.",
        kind="decision",
        tags=("dual-write",),
        source_conversation="conversation-backfill",
        actor_id="actor-backfill",
        idempotency_key="idempotency-replace-building-backfill",
        payload_digest="keyed-payload-digest-replace-building-backfill",
        occurred_at="2026-08-02T04:08:00+00:00",
        embedding_blobs={1: b"new-active-vector", 2: b"new-building-vector"},
    )

    assert result.revision == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """
            SELECT generation, scope, provider, model, dimension, embedding_blob
            FROM memory_embeddings WHERE memory_id = ? ORDER BY generation
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (
                1,
                "project:recall",
                "test-provider",
                "test-embedding-model",
                2,
                b"new-active-vector",
            ),
            (
                2,
                "project:recall",
                "test-provider",
                "test-embedding-model-v2",
                2,
                b"new-building-vector",
            ),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _seed_scope_move_target(
    path: Path, backup_path: Path, *, grant_destination: bool, mirror_profiles: bool
) -> RecallMCPRepository:
    repository = _seed_replace_target(path, backup_path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if grant_destination:
            conn.execute(
                """
                UPDATE client_grants
                SET memory_scope_patterns_json = ?
                WHERE grant_id = ?
                """,
                ('["project:recall","project:archive"]', WRITE_GRANT_ID),
            )
        if mirror_profiles:
            for profile in conn.execute(
                """
                SELECT generation, provider, endpoint_identity_hash, model,
                       dimension, status, created_at, activated_at, retired_at
                FROM embedding_profiles
                WHERE owner_id = ? AND scope = ? ORDER BY generation
                """,
                (BOOTSTRAP.owner_id, "project:recall"),
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO embedding_profiles (
                        owner_id, scope, generation, provider,
                        endpoint_identity_hash, model, dimension, status,
                        created_at, activated_at, retired_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (BOOTSTRAP.owner_id, "project:archive", *profile),
                )
    return repository


def _attempt_scope_move(
    repository: RecallMCPRepository, *, expected_revision: int = 1
) -> None:
    repository.replace_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        new_scope="project:archive",
        memory_id=MEMORY_ID,
        expected_revision=expected_revision,
        content="Scope move must fail closed without partial writes.",
        kind="decision",
        tags=("scope-move",),
        source_conversation="conversation-scope-move-guard",
        actor_id="actor-scope-move-guard",
        idempotency_key="idempotency-replace-scope-move-guard",
        payload_digest="keyed-payload-digest-replace-scope-move-guard",
        occurred_at="2026-08-02T04:08:45+00:00",
        embedding_blobs={1: b"archive-active", 2: b"archive-building"},
    )


def test_replace_scope_move_requires_destination_authorization(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-scope-move-auth.db"
    repository = _seed_scope_move_target(
        db_path,
        tmp_path / "authority-scope-move-auth-backup.db",
        grant_destination=False,
        mirror_profiles=True,
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(PermissionError, match="not authorized"):
        _attempt_scope_move(repository)

    assert _snapshot_memory_state(db_path) == before


def test_replace_scope_move_fails_closed_when_destination_profiles_differ(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-scope-move-parity.db"
    repository = _seed_scope_move_target(
        db_path,
        tmp_path / "authority-scope-move-parity-backup.db",
        grant_destination=True,
        mirror_profiles=False,
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        ValueError, match="source and destination embedding profiles must match"
    ):
        _attempt_scope_move(repository)

    assert _snapshot_memory_state(db_path) == before


def test_replace_scope_move_fails_closed_when_physical_vectors_exist(
    tmp_path: Path,
) -> None:
    pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-scope-move-physical.db"
    repository = _seed_scope_move_target(
        db_path,
        tmp_path / "authority-scope-move-physical-backup.db",
        grant_destination=True,
        mirror_profiles=True,
    )
    table_names = _attach_generation_vector_tables(db_path)
    before_memory = _snapshot_memory_state(db_path)
    before_generation = _snapshot_generation_state(db_path, table_names)

    with pytest.raises(
        RuntimeError, match="scope move with physical vectors is not yet supported"
    ):
        _attempt_scope_move(repository)

    assert _snapshot_memory_state(db_path) == before_memory
    assert _snapshot_generation_state(db_path, table_names) == before_generation


def test_replace_scope_move_rejects_stale_revision_without_side_effects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-scope-move-stale.db"
    repository = _seed_scope_move_target(
        db_path,
        tmp_path / "authority-scope-move-stale-backup.db",
        grant_destination=True,
        mirror_profiles=True,
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(RevisionConflictError) as conflict:
        _attempt_scope_move(repository, expected_revision=9)

    assert conflict.value.current_revision == 1
    assert _snapshot_memory_state(db_path) == before


def test_replace_scope_move_rolls_back_every_row_when_final_stage_fails(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-scope-move-rollback.db"
    _seed_scope_move_target(
        db_path,
        tmp_path / "authority-scope-move-rollback-backup.db",
        grant_destination=True,
        mirror_profiles=True,
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        RuntimeError, match="deterministic failure after all derived writes"
    ):
        _attempt_scope_move(_FailAfterIdempotencyRepository(db_path))

    assert _snapshot_memory_state(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_replace_rolls_back_every_row_when_final_stage_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-replace-rollback.db"
    _seed_replace_target(db_path, tmp_path / "authority-replace-rollback-backup.db")
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        RuntimeError, match="deterministic failure after all derived writes"
    ):
        _FailAfterIdempotencyRepository(db_path).replace_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=1,
            content="This replacement must be rolled back from every index.",
            kind="semantic",
            tags=("rollback",),
            source_conversation="conversation-rollback",
            actor_id="actor-rollback",
            idempotency_key="idempotency-replace-rollback",
            payload_digest="keyed-payload-digest-replace-rollback",
            occurred_at="2026-08-02T04:09:00+00:00",
            embedding_blobs={1: b"rollback-active", 2: b"rollback-building"},
        )

    assert _snapshot_memory_state(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_replace_rejects_stale_revision_without_side_effects(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-replace-conflict.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-replace-conflict-backup.db"
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(RevisionConflictError) as conflict:
        repository.replace_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=7,
            content="A stale writer must never change canonical data.",
            kind="semantic",
            tags=("stale",),
            source_conversation="conversation-stale",
            actor_id="actor-stale",
            idempotency_key="idempotency-replace-stale",
            payload_digest="keyed-payload-digest-replace-stale",
            occurred_at="2026-08-02T04:10:00+00:00",
            embedding_blobs={1: b"stale-active", 2: b"stale-building"},
        )

    assert conflict.value.current_revision == 1
    assert _snapshot_memory_state(db_path) == before


def test_soft_remove_preserves_reversible_rows_and_hides_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-remove.db"
    repository = _seed_replace_target(db_path, tmp_path / "authority-remove-backup.db")
    before = _snapshot_memory_state(db_path)

    result = repository.remove_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=1,
        actor_id="actor-remover",
        idempotency_key="idempotency-remove-success",
        payload_digest="keyed-payload-digest-remove-success",
        occurred_at="2026-08-02T04:11:00+00:00",
    )

    assert result.memory_id == MEMORY_ID
    assert result.revision == 2
    assert result.deleted_at == "2026-08-02T04:11:00+00:00"
    after = _snapshot_memory_state(db_path)
    for preserved in ("memories", "keywords", "fts", "embeddings"):
        assert after[preserved] == before[preserved]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """
            SELECT creator_grant_id, revision, scope, source_client, actor_id,
                   created_at, updated_at, deleted_at
            FROM memory_metadata WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchone() == (
            WRITE_GRANT_ID,
            2,
            "project:recall",
            "kiro",
            "actor-remover",
            "2026-08-02T04:06:00+00:00",
            "2026-08-02T04:11:00+00:00",
            "2026-08-02T04:11:00+00:00",
        )
        assert conn.execute(
            """
            SELECT revision, operation, actor_id, source_client, payload_digest
            FROM memory_events WHERE memory_id = ? ORDER BY revision
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (
                1,
                "add",
                "actor-original",
                "kiro",
                "keyed-payload-digest-add-before-replace",
            ),
            (
                2,
                "remove",
                "actor-remover",
                "kiro",
                "keyed-payload-digest-remove-success",
            ),
        ]
        remove_idempotency = conn.execute(
            """
            SELECT operation, idempotency_key, result_json
            FROM mcp_idempotency
            WHERE memory_id = ? AND operation = 'remove'
            """,
            (MEMORY_ID,),
        ).fetchone()
        assert remove_idempotency[:2] == (
            "remove",
            "idempotency-remove-success",
        )
        assert json.loads(remove_idempotency[2]) == {
            "deleted_at": "2026-08-02T04:11:00+00:00",
            "memory_id": MEMORY_ID,
            "revision": 2,
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert repository.search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Original",
    ) == ()


def test_soft_remove_rolls_back_every_row_when_final_stage_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-remove-rollback.db"
    _seed_replace_target(db_path, tmp_path / "authority-remove-rollback-backup.db")
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        RuntimeError, match="deterministic failure after all derived writes"
    ):
        _FailAfterIdempotencyRepository(db_path).remove_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=1,
            actor_id="actor-remove-rollback",
            idempotency_key="idempotency-remove-rollback",
            payload_digest="keyed-payload-digest-remove-rollback",
            occurred_at="2026-08-02T04:12:00+00:00",
        )

    assert _snapshot_memory_state(db_path) == before
    visible = RecallMCPRepository(db_path).search_scoped(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        query="Original",
    )
    assert [(row.memory_id, row.revision) for row in visible] == [(MEMORY_ID, 1)]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_soft_remove_rejects_stale_revision_without_side_effects(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-remove-conflict.db"
    repository = _seed_replace_target(
        db_path, tmp_path / "authority-remove-conflict-backup.db"
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(RevisionConflictError) as conflict:
        repository.remove_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=7,
            actor_id="actor-remove-stale",
            idempotency_key="idempotency-remove-stale",
            payload_digest="keyed-payload-digest-remove-stale",
            occurred_at="2026-08-02T04:13:00+00:00",
        )

    assert conflict.value.current_revision == 1
    assert _snapshot_memory_state(db_path) == before


def _seed_tombstone_target(path: Path, backup_path: Path) -> RecallMCPRepository:
    repository = _seed_replace_target(path, backup_path)
    repository.remove_memory(
        grant_id=WRITE_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=1,
        actor_id="actor-remover-before-restore",
        idempotency_key="idempotency-remove-before-restore",
        payload_digest="keyed-payload-digest-remove-before-restore",
        occurred_at="2026-08-02T04:14:00+00:00",
    )
    return repository


def test_owner_admin_restore_rebuilds_indexes_and_advances_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-restore.db"
    repository = _seed_tombstone_target(
        db_path, tmp_path / "authority-restore-backup.db"
    )
    tombstoned = _snapshot_memory_state(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM keywords WHERE memory_id = ?", (MEMORY_ID,))
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (MEMORY_ID,))

    result = repository.restore_memory(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=2,
        actor_id="actor-owner-admin",
        idempotency_key="idempotency-restore-success",
        payload_digest="keyed-payload-digest-restore-success",
        occurred_at="2026-08-02T04:15:00+00:00",
    )

    assert result.memory_id == MEMORY_ID
    assert result.revision == 3
    assert result.restored_at == "2026-08-02T04:15:00+00:00"
    after = _snapshot_memory_state(db_path)
    assert after["memories"] == tombstoned["memories"]
    assert after["embeddings"] == tombstoned["embeddings"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """
            SELECT creator_grant_id, revision, source_client, actor_id,
                   created_at, updated_at, deleted_at
            FROM memory_metadata WHERE memory_id = ?
            """,
            (MEMORY_ID,),
        ).fetchone() == (
            WRITE_GRANT_ID,
            3,
            "admin-cli",
            "actor-owner-admin",
            "2026-08-02T04:06:00+00:00",
            "2026-08-02T04:15:00+00:00",
            None,
        )
        assert "original" in {
            row[0]
            for row in conn.execute(
                "SELECT keyword FROM keywords WHERE memory_id = ?", (MEMORY_ID,)
            )
        }
        assert conn.execute(
            "SELECT content FROM memories_fts WHERE id = ?", (MEMORY_ID,)
        ).fetchall() == [
            ("Original transactional content must disappear from every index.",)
        ]
        assert conn.execute(
            """
            SELECT revision, operation, grant_id, actor_id
            FROM memory_events WHERE memory_id = ? ORDER BY revision
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, "add", WRITE_GRANT_ID, "actor-original"),
            (2, "remove", WRITE_GRANT_ID, "actor-remover-before-restore"),
            (3, "restore", ADMIN_GRANT_ID, "actor-owner-admin"),
        ]
        restore_result = conn.execute(
            """
            SELECT result_json FROM mcp_idempotency
            WHERE grant_id = ? AND operation = 'restore' AND idempotency_key = ?
            """,
            (ADMIN_GRANT_ID, "idempotency-restore-success"),
        ).fetchone()
        assert json.loads(restore_result[0]) == {
            "memory_id": MEMORY_ID,
            "restored_at": "2026-08-02T04:15:00+00:00",
            "revision": 3,
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    visible = repository.search_scoped(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        query="Original",
    )
    assert [(row.memory_id, row.revision) for row in visible] == [(MEMORY_ID, 3)]


def test_owner_admin_restore_rolls_back_rebuilt_indexes_on_final_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-restore-rollback.db"
    _seed_tombstone_target(db_path, tmp_path / "authority-restore-rollback-backup.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM keywords WHERE memory_id = ?", (MEMORY_ID,))
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (MEMORY_ID,))
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        RuntimeError, match="deterministic failure after all derived writes"
    ):
        _FailAfterIdempotencyRepository(db_path).restore_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=2,
            actor_id="actor-restore-rollback",
            idempotency_key="idempotency-restore-rollback",
            payload_digest="keyed-payload-digest-restore-rollback",
            occurred_at="2026-08-02T04:16:00+00:00",
        )

    assert _snapshot_memory_state(db_path) == before
    assert RecallMCPRepository(db_path).search_scoped(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        query="Original",
    ) == ()


def test_owner_admin_restore_rejects_stale_revision_without_side_effects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-restore-stale.db"
    repository = _seed_tombstone_target(
        db_path, tmp_path / "authority-restore-stale-backup.db"
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(RevisionConflictError) as conflict:
        repository.restore_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=7,
            actor_id="actor-restore-stale",
            idempotency_key="idempotency-restore-stale",
            payload_digest="keyed-payload-digest-restore-stale",
            occurred_at="2026-08-02T04:17:00+00:00",
        )

    assert conflict.value.current_revision == 2
    assert _snapshot_memory_state(db_path) == before


def test_restore_requires_owner_admin_without_side_effects(tmp_path: Path) -> None:
    db_path = tmp_path / "authority-restore-auth.db"
    repository = _seed_tombstone_target(
        db_path, tmp_path / "authority-restore-auth-backup.db"
    )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(PermissionError, match="not authorized"):
        repository.restore_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=2,
            actor_id="actor-non-admin",
            idempotency_key="idempotency-restore-non-admin",
            payload_digest="keyed-payload-digest-restore-non-admin",
            occurred_at="2026-08-02T04:18:00+00:00",
        )

    assert _snapshot_memory_state(db_path) == before


class _FailAfterPurgeDeletesRepository(RecallMCPRepository):
    def _after_write_stage(self, stage: str, conn: sqlite3.Connection) -> None:
        super()._after_write_stage(stage, conn)
        if stage == "purge_memory":
            raise RuntimeError("deterministic failure after purge deletes")


def _seed_restored_target(
    path: Path, backup_path: Path, *, create_legacy_vector_table: bool = True
) -> RecallMCPRepository:
    repository = _seed_tombstone_target(path, backup_path)
    repository.restore_memory(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=2,
        actor_id="actor-admin-before-purge",
        idempotency_key="idempotency-restore-before-purge",
        payload_digest="keyed-payload-digest-restore-before-purge",
        occurred_at="2026-08-02T04:19:00+00:00",
    )
    if create_legacy_vector_table:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE vec_embeddings(id TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
            )
            conn.execute(
                "INSERT INTO vec_embeddings(id, embedding) VALUES (?, ?)",
                (MEMORY_ID, b"legacy-vector-content"),
            )
    return repository


def _snapshot_purge_target(path: Path) -> dict[str, list[tuple[object, ...]]]:
    snapshot = _snapshot_memory_state(path)
    with sqlite3.connect(path) as conn:
        snapshot["vec_embeddings"] = conn.execute(
            "SELECT id, embedding FROM vec_embeddings WHERE id = ?", (MEMORY_ID,)
        ).fetchall()
    return snapshot


def test_owner_admin_purge_deletes_content_and_keeps_final_audit_event(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-purge.db"
    repository = _seed_restored_target(
        db_path, tmp_path / "authority-purge-backup.db"
    )

    result = repository.purge_memory(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=3,
        actor_id="actor-owner-admin-purge",
        idempotency_key="idempotency-purge-success",
        payload_digest="keyed-content-free-purge-event-digest",
        deny_payload_digest="keyed-content-free-deny-digest",
        occurred_at="2026-08-02T04:20:00+00:00",
    )

    assert result.memory_id == MEMORY_ID
    assert result.final_revision == 4
    assert result.purged_at == "2026-08-02T04:20:00+00:00"
    with sqlite3.connect(db_path) as conn:
        for table, column in (
            ("memories", "id"),
            ("keywords", "memory_id"),
            ("memories_fts", "id"),
            ("memory_metadata", "memory_id"),
            ("memory_embeddings", "memory_id"),
            ("vec_embeddings", "id"),
        ):
            assert conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?',
                (MEMORY_ID,),
            ).fetchone() == (0,)
        assert conn.execute(
            """
            SELECT revision, operation, grant_id, actor_id, source_client,
                   payload_digest
            FROM memory_events WHERE memory_id = ? ORDER BY revision
            """,
            (MEMORY_ID,),
        ).fetchall() == [
            (1, "add", WRITE_GRANT_ID, "actor-original", "kiro", "keyed-payload-digest-add-before-replace"),
            (2, "remove", WRITE_GRANT_ID, "actor-remover-before-restore", "kiro", "keyed-payload-digest-remove-before-restore"),
            (3, "restore", ADMIN_GRANT_ID, "actor-admin-before-purge", "admin-cli", "keyed-payload-digest-restore-before-purge"),
            (4, "purge", ADMIN_GRANT_ID, "actor-owner-admin-purge", "admin-cli", "keyed-content-free-purge-event-digest"),
        ]
        deny_rows = conn.execute(
            """
            SELECT operation, result_json, payload_digest, purged_at
            FROM mcp_idempotency WHERE owner_id = ? AND memory_id = ?
            ORDER BY operation, idempotency_key
            """,
            (BOOTSTRAP.owner_id, MEMORY_ID),
        ).fetchall()
        assert {row[0] for row in deny_rows} == {"add", "remove", "restore", "purge"}
        assert all(row[1] is None for row in deny_rows)
        assert {row[2] for row in deny_rows} == {"keyed-content-free-deny-digest"}
        assert {row[3] for row in deny_rows} == {"2026-08-02T04:20:00+00:00"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert repository.search_scoped(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        query="Original",
    ) == ()


def test_owner_admin_purge_deletes_real_sqlite_vec_projection(tmp_path: Path) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-purge-sqlite-vec.db"
    repository = _seed_restored_target(
        db_path,
        tmp_path / "authority-purge-sqlite-vec-backup.db",
        create_legacy_vector_table=False,
    )
    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            "CREATE VIRTUAL TABLE vec_embeddings USING vec0("
            "id TEXT PRIMARY KEY, embedding float[2] distance_metric=cosine)"
        )
        conn.execute(
            "INSERT INTO vec_embeddings(id, embedding) VALUES (?, ?)",
            (MEMORY_ID, struct.pack("<2f", 0.25, 0.75)),
        )

    repository.purge_memory(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=3,
        actor_id="actor-owner-admin-purge-vector",
        idempotency_key="idempotency-purge-vector-success",
        payload_digest="keyed-content-free-purge-vector-event",
        deny_payload_digest="keyed-content-free-purge-vector-deny",
        occurred_at="2026-08-02T04:20:30+00:00",
    )

    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        assert conn.execute(
            "SELECT COUNT(*) FROM vec_embeddings WHERE id = ?", (MEMORY_ID,)
        ).fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _register_generation_vector_rows(path: Path) -> tuple[str, ...]:
    import sqlite_vec

    table_names: list[str] = []
    with sqlite3.connect(path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for generation in (1, 2):
            table_name = f"vec_generation_{generation}"
            table_names.append(table_name)
            conn.execute(
                f'CREATE VIRTUAL TABLE "{table_name}" '
                "USING vec0(embedding float[2] distance_metric=cosine)"
            )
            cursor = conn.execute(
                f'INSERT INTO "{table_name}"(embedding) VALUES (?)',
                (struct.pack("<2f", generation / 10, generation / 5),),
            )
            vector_rowid = cursor.lastrowid
            assert vector_rowid is not None
            conn.execute(
                """
                INSERT INTO vector_tables (
                    owner_id, scope, generation, table_name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    BOOTSTRAP.owner_id,
                    "project:recall",
                    generation,
                    table_name,
                    "2026-08-02T04:20:40+00:00",
                ),
            )
            conn.execute(
                """
                UPDATE memory_embeddings
                SET vector_rowid = ?
                WHERE memory_id = ? AND generation = ?
                """,
                (vector_rowid, MEMORY_ID, generation),
            )
    return tuple(table_names)


def test_owner_admin_purge_deletes_registered_generation_vector_rows(
    tmp_path: Path,
) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-purge-generation-vectors.db"
    repository = _seed_restored_target(
        db_path,
        tmp_path / "authority-purge-generation-vectors-backup.db",
        create_legacy_vector_table=False,
    )
    table_names = _register_generation_vector_rows(db_path)

    repository.purge_memory(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        memory_id=MEMORY_ID,
        expected_revision=3,
        actor_id="actor-owner-admin-purge-generations",
        idempotency_key="idempotency-purge-generations-success",
        payload_digest="keyed-content-free-purge-generations-event",
        deny_payload_digest="keyed-content-free-purge-generations-deny",
        occurred_at="2026-08-02T04:20:50+00:00",
    )

    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for table_name in table_names:
            assert conn.execute(
                f'SELECT COUNT(*) FROM "{table_name}"'
            ).fetchone() == (0,)


def test_owner_admin_purge_rolls_back_registered_generation_vector_deletes(
    tmp_path: Path,
) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-purge-generation-vector-rollback.db"
    _seed_restored_target(
        db_path,
        tmp_path / "authority-purge-generation-vector-rollback-backup.db",
        create_legacy_vector_table=False,
    )
    table_names = _register_generation_vector_rows(db_path)
    before = _snapshot_memory_state(db_path)

    with pytest.raises(RuntimeError, match="deterministic failure after purge deletes"):
        _FailAfterPurgeDeletesRepository(db_path).purge_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=3,
            actor_id="actor-purge-generation-rollback",
            idempotency_key="idempotency-purge-generation-rollback",
            payload_digest="keyed-purge-generation-rollback-event",
            deny_payload_digest="keyed-purge-generation-rollback-deny",
            occurred_at="2026-08-02T04:20:55+00:00",
        )

    assert _snapshot_memory_state(db_path) == before
    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for table_name in table_names:
            assert conn.execute(
                f'SELECT COUNT(*) FROM "{table_name}"'
            ).fetchone() == (1,)


def test_owner_admin_purge_fails_closed_for_invalid_registered_vector_table(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-purge-invalid-generation-table.db"
    repository = _seed_restored_target(
        db_path,
        tmp_path / "authority-purge-invalid-generation-table-backup.db",
        create_legacy_vector_table=False,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            'CREATE TABLE not_a_vector_table('
            'id INTEGER PRIMARY KEY, "using vec0" BLOB NOT NULL)'
        )
        cursor = conn.execute(
            'INSERT INTO not_a_vector_table("using vec0") VALUES (?)',
            (b"must-survive",),
        )
        vector_rowid = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO vector_tables (
                owner_id, scope, generation, table_name, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP.owner_id,
                "project:recall",
                1,
                "not_a_vector_table",
                "2026-08-02T04:20:57+00:00",
            ),
        )
        conn.execute(
            """
            UPDATE memory_embeddings SET vector_rowid = ?
            WHERE memory_id = ? AND generation = 1
            """,
            (vector_rowid, MEMORY_ID),
        )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        RuntimeError, match="registered generation vector table is invalid"
    ):
        repository.purge_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=3,
            actor_id="actor-purge-invalid-generation-table",
            idempotency_key="idempotency-purge-invalid-generation-table",
            payload_digest="keyed-purge-invalid-generation-table-event",
            deny_payload_digest="keyed-purge-invalid-generation-table-deny",
            occurred_at="2026-08-02T04:20:59+00:00",
        )

    assert _snapshot_memory_state(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM not_a_vector_table"
        ).fetchone() == (1,)


def test_owner_admin_purge_fails_closed_for_missing_registered_vector_rowid(
    tmp_path: Path,
) -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db_path = tmp_path / "authority-purge-missing-vector-rowid.db"
    repository = _seed_restored_target(
        db_path,
        tmp_path / "authority-purge-missing-vector-rowid-backup.db",
        create_legacy_vector_table=False,
    )
    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            "CREATE VIRTUAL TABLE vec_missing_rowid "
            "USING vec0(embedding float[2] distance_metric=cosine)"
        )
        conn.execute(
            "INSERT INTO vec_missing_rowid(embedding) VALUES (?)",
            (struct.pack("<2f", 0.25, 0.75),),
        )
        conn.execute(
            """
            INSERT INTO vector_tables (
                owner_id, scope, generation, table_name, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP.owner_id,
                "project:recall",
                1,
                "vec_missing_rowid",
                "2026-08-02T04:20:58+00:00",
            ),
        )
    before = _snapshot_memory_state(db_path)

    with pytest.raises(
        RuntimeError, match="registered generation vector is missing vector_rowid"
    ):
        repository.purge_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=3,
            actor_id="actor-purge-missing-vector-rowid",
            idempotency_key="idempotency-purge-missing-vector-rowid",
            payload_digest="keyed-purge-missing-vector-rowid-event",
            deny_payload_digest="keyed-purge-missing-vector-rowid-deny",
            occurred_at="2026-08-02T04:20:59+00:00",
        )

    assert _snapshot_memory_state(db_path) == before
    with sqlite3.connect(db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        assert conn.execute(
            "SELECT COUNT(*) FROM vec_missing_rowid"
        ).fetchone() == (1,)


def test_owner_admin_purge_rolls_back_final_event_scrub_and_all_deletes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-purge-rollback.db"
    _seed_restored_target(db_path, tmp_path / "authority-purge-rollback-backup.db")
    before = _snapshot_purge_target(db_path)

    with pytest.raises(RuntimeError, match="deterministic failure after purge deletes"):
        _FailAfterPurgeDeletesRepository(db_path).purge_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=3,
            actor_id="actor-purge-rollback",
            idempotency_key="idempotency-purge-rollback",
            payload_digest="keyed-content-free-purge-rollback-event",
            deny_payload_digest="keyed-content-free-purge-rollback-deny",
            occurred_at="2026-08-02T04:21:00+00:00",
        )

    assert _snapshot_purge_target(db_path) == before
    visible = RecallMCPRepository(db_path).search_scoped(
        grant_id=ADMIN_GRANT_ID,
        scope="project:recall",
        query="Original",
    )
    assert [(row.memory_id, row.revision) for row in visible] == [(MEMORY_ID, 3)]


def test_owner_admin_purge_rejects_stale_revision_without_side_effects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-purge-stale.db"
    repository = _seed_restored_target(
        db_path, tmp_path / "authority-purge-stale-backup.db"
    )
    before = _snapshot_purge_target(db_path)

    with pytest.raises(RevisionConflictError) as conflict:
        repository.purge_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=2,
            actor_id="actor-purge-stale",
            idempotency_key="idempotency-purge-stale",
            payload_digest="keyed-content-free-purge-stale-event",
            deny_payload_digest="keyed-content-free-purge-stale-deny",
            occurred_at="2026-08-02T04:22:00+00:00",
        )

    assert conflict.value.current_revision == 3
    assert _snapshot_purge_target(db_path) == before


def test_purge_requires_owner_admin_and_rolls_back_final_revision_collision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "authority-purge-auth-collision.db"
    repository = _seed_restored_target(
        db_path, tmp_path / "authority-purge-auth-collision-backup.db"
    )
    before = _snapshot_purge_target(db_path)

    with pytest.raises(PermissionError, match="not authorized"):
        repository.purge_memory(
            grant_id=WRITE_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=3,
            actor_id="actor-purge-non-admin",
            idempotency_key="idempotency-purge-non-admin",
            payload_digest="keyed-content-free-purge-non-admin-event",
            deny_payload_digest="keyed-content-free-purge-non-admin-deny",
            occurred_at="2026-08-02T04:23:00+00:00",
        )
    assert _snapshot_purge_target(db_path) == before

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO memory_events (
                event_id, memory_id, owner_id, grant_id, revision, operation,
                actor_id, source_client, occurred_at, payload_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-existing-revision-four",
                MEMORY_ID,
                BOOTSTRAP.owner_id,
                ADMIN_GRANT_ID,
                4,
                "existing-final",
                "actor-existing-final",
                "admin-cli",
                "2026-08-02T04:23:30+00:00",
                "keyed-existing-final-digest",
            ),
        )
    collision_before = _snapshot_purge_target(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        repository.purge_memory(
            grant_id=ADMIN_GRANT_ID,
            scope="project:recall",
            memory_id=MEMORY_ID,
            expected_revision=3,
            actor_id="actor-purge-collision",
            idempotency_key="idempotency-purge-collision",
            payload_digest="keyed-content-free-purge-collision-event",
            deny_payload_digest="keyed-content-free-purge-collision-deny",
            occurred_at="2026-08-02T04:24:00+00:00",
        )
    assert _snapshot_purge_target(db_path) == collision_before
