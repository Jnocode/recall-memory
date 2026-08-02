"""Concurrency and generic idempotency regressions for the Recall MCP authority.

Every test here uses real SQLite connections (one per repository call, exactly as
production does) and a thread or process barrier.  Nothing is mocked: the point
is to prove the transactional guarantees under genuine contention.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from recall.mcp_repository import (  # noqa: E402
    AddMemoryResult,
    IdempotencyKeyReusedError,
    RecallMCPRepository,
    RemoveMemoryResult,
    ReplaceMemoryResult,
    RevisionConflictError,
)
from test_mcp_repository import (  # noqa: E402
    ADMIN_GRANT_ID,
    BOOTSTRAP,
    WRITE_GRANT_ID,
    _create_migrated_authority,
)

SCOPE = "project:recall"
OTHER_OWNER_ID = "owner-concurrency-intruder"
OTHER_GRANT_ID = "grant-concurrency-intruder"
SRC_PATH = str(Path(__file__).resolve().parents[1] / "src")

_ROW_TABLES = (
    "memories",
    "keywords",
    "fts",
    "metadata",
    "embeddings",
    "events",
    "idempotency",
)


def _authority(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / f"{name}.db"
    _create_migrated_authority(db_path, tmp_path / f"{name}-backup.db")
    return db_path


def _add(
    repository: RecallMCPRepository,
    *,
    memory_id: str,
    idempotency_key: str,
    payload_digest: str = "digest-shared",
    content: str = "Shared authority content for concurrency regression.",
    occurred_at: str = "2026-08-02T05:00:00+00:00",
    grant_id: str = WRITE_GRANT_ID,
    scope: str = SCOPE,
) -> AddMemoryResult:
    return repository.add_memory(
        grant_id=grant_id,
        scope=scope,
        memory_id=memory_id,
        content=content,
        kind="decision",
        tags=("concurrency",),
        source_conversation="conversation-concurrency",
        actor_id="actor-concurrency",
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        occurred_at=occurred_at,
        embedding_blobs={1: b"active-generation-vector"},
    )


def _replace(
    repository: RecallMCPRepository,
    *,
    memory_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload_digest: str = "digest-replace",
    content: str = "Replaced authority content.",
    occurred_at: str = "2026-08-02T05:05:00+00:00",
    grant_id: str = WRITE_GRANT_ID,
    scope: str = SCOPE,
) -> ReplaceMemoryResult:
    return repository.replace_memory(
        grant_id=grant_id,
        scope=scope,
        memory_id=memory_id,
        expected_revision=expected_revision,
        content=content,
        kind="decision",
        tags=("concurrency",),
        source_conversation="conversation-concurrency",
        actor_id="actor-concurrency",
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        occurred_at=occurred_at,
        embedding_blobs={1: b"replaced-generation-vector"},
    )


def _remove(
    repository: RecallMCPRepository,
    *,
    memory_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload_digest: str = "digest-remove",
    occurred_at: str = "2026-08-02T05:10:00+00:00",
    grant_id: str = WRITE_GRANT_ID,
    scope: str = SCOPE,
) -> RemoveMemoryResult:
    return repository.remove_memory(
        grant_id=grant_id,
        scope=scope,
        memory_id=memory_id,
        expected_revision=expected_revision,
        actor_id="actor-concurrency",
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        occurred_at=occurred_at,
    )


def _row_counts(path: Path, memory_id: str) -> dict[str, int]:
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


def _full_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Snapshot every table an unauthorized replay must never touch."""

    queries = {
        "memories": "SELECT * FROM memories ORDER BY id",
        "keywords": "SELECT * FROM keywords ORDER BY keyword, memory_id",
        "fts": "SELECT rowid, content, id FROM memories_fts ORDER BY rowid",
        "metadata": "SELECT * FROM memory_metadata ORDER BY memory_id",
        "embeddings": (
            "SELECT * FROM memory_embeddings ORDER BY memory_id, generation"
        ),
        "events": "SELECT * FROM memory_events ORDER BY memory_id, revision",
        "idempotency": (
            "SELECT * FROM mcp_idempotency "
            "ORDER BY grant_id, operation, idempotency_key"
        ),
    }
    with sqlite3.connect(path) as conn:
        return {name: conn.execute(sql).fetchall() for name, sql in queries.items()}


def _run_barrier(worker, count: int = 2) -> tuple[dict, dict]:
    """Run ``worker(index)`` on ``count`` threads released by one barrier."""

    results: dict[int, object] = {}
    errors: dict[int, BaseException] = {}
    barrier = threading.Barrier(count, timeout=30)

    def target(index: int) -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:  # pragma: no cover - guard
            errors[index] = exc
            return
        try:
            results[index] = worker(index)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            errors[index] = exc

    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    assert not any(thread.is_alive() for thread in threads), "worker thread deadlocked"
    return results, errors


# --------------------------------------------------------------------------
# 1.13 two real connections + barrier
# --------------------------------------------------------------------------


def test_concurrent_adds_on_two_real_connections_both_commit_intact(
    tmp_path: Path,
) -> None:
    db_path = _authority(tmp_path, "concurrent-add")

    def worker(index: int) -> AddMemoryResult:
        # A fresh repository per thread: every call opens its own connection,
        # exactly like two MCP requests hitting the same authority file.
        repository = RecallMCPRepository(db_path)
        return _add(
            repository,
            memory_id=f"memory-concurrent-{index}",
            idempotency_key=f"key-concurrent-{index}",
            payload_digest=f"digest-concurrent-{index}",
            content=f"Concurrent transactional add number {index}.",
        )

    results, errors = _run_barrier(worker)

    assert errors == {}
    assert sorted(result.memory_id for result in results.values()) == [
        "memory-concurrent-0",
        "memory-concurrent-1",
    ]
    assert {result.revision for result in results.values()} == {1}
    for index in (0, 1):
        assert _row_counts(db_path, f"memory-concurrent-{index}") == {
            "memories": 1,
            "keywords": 3,
            "fts": 1,
            "metadata": 1,
            "embeddings": 1,
            "events": 1,
            "idempotency": 1,
        }
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --------------------------------------------------------------------------
# 1.14 same key + same payload commits once and replays the same result
# --------------------------------------------------------------------------


def test_concurrent_same_key_same_payload_commits_once_and_replays_result(
    tmp_path: Path,
) -> None:
    db_path = _authority(tmp_path, "same-key-same-payload")
    memory_id = "memory-single-commit"

    def worker(_index: int) -> AddMemoryResult:
        return _add(
            RecallMCPRepository(db_path),
            memory_id=memory_id,
            idempotency_key="key-single-commit",
            payload_digest="digest-single-commit",
        )

    results, errors = _run_barrier(worker)

    assert errors == {}
    assert results[0] == results[1]
    assert results[0].revision == 1
    assert _row_counts(db_path, memory_id) == {
        "memories": 1,
        "keywords": 5,
        "fts": 1,
        "metadata": 1,
        "embeddings": 1,
        "events": 1,
        "idempotency": 1,
    }

    # A later sequential replay must return the identical committed result.
    replayed = _add(
        RecallMCPRepository(db_path),
        memory_id=memory_id,
        idempotency_key="key-single-commit",
        payload_digest="digest-single-commit",
        occurred_at="2026-08-02T09:30:00+00:00",
    )
    assert replayed == results[0]
    assert _row_counts(db_path, memory_id)["events"] == 1


def test_every_write_operation_replays_its_committed_result(tmp_path: Path) -> None:
    db_path = _authority(tmp_path, "replay-all-operations")
    repository = RecallMCPRepository(db_path)
    memory_id = "memory-replay-all"

    added = _add(repository, memory_id=memory_id, idempotency_key="key-add")
    assert _add(repository, memory_id=memory_id, idempotency_key="key-add") == added

    replaced = _replace(
        repository,
        memory_id=memory_id,
        expected_revision=1,
        idempotency_key="key-replace",
    )
    assert (
        _replace(
            repository,
            memory_id=memory_id,
            expected_revision=1,
            idempotency_key="key-replace",
        )
        == replaced
    )
    # Replaying with a now-stale expected revision still returns the stored
    # result instead of a spurious conflict.
    assert (
        _replace(
            repository,
            memory_id=memory_id,
            expected_revision=2,
            idempotency_key="key-replace",
        )
        == replaced
    )

    removed = _remove(
        repository,
        memory_id=memory_id,
        expected_revision=2,
        idempotency_key="key-remove",
    )
    assert (
        _remove(
            repository,
            memory_id=memory_id,
            expected_revision=2,
            idempotency_key="key-remove",
        )
        == removed
    )

    assert (added.revision, replaced.revision, removed.revision) == (1, 2, 3)
    assert _row_counts(db_path, memory_id) == {
        "memories": 1,
        "keywords": 3,
        "fts": 1,
        "metadata": 1,
        "embeddings": 1,
        "events": 3,
        "idempotency": 3,
    }


# --------------------------------------------------------------------------
# 1.15 same key + different payload fails closed
# --------------------------------------------------------------------------


def test_same_key_with_different_payload_fails_closed(tmp_path: Path) -> None:
    db_path = _authority(tmp_path, "same-key-other-payload")
    repository = RecallMCPRepository(db_path)
    memory_id = "memory-key-reuse"
    _add(
        repository,
        memory_id=memory_id,
        idempotency_key="key-reused",
        payload_digest="digest-original",
        content="Original committed content for the reused key.",
    )
    before = _full_snapshot(db_path)

    with pytest.raises(IdempotencyKeyReusedError) as different_payload:
        _add(
            repository,
            memory_id=memory_id,
            idempotency_key="key-reused",
            payload_digest="digest-tampered",
            content="Attacker supplied content that must never commit.",
        )
    assert _full_snapshot(db_path) == before

    with pytest.raises(IdempotencyKeyReusedError):
        # Same key, same digest, but a different target memory id.
        _add(
            repository,
            memory_id="memory-key-reuse-other",
            idempotency_key="key-reused",
            payload_digest="digest-tampered-2",
        )
    assert _full_snapshot(db_path) == before
    assert _row_counts(db_path, "memory-key-reuse-other") == dict.fromkeys(_ROW_TABLES, 0)

    with pytest.raises(IdempotencyKeyReusedError):
        # Same key reused for a different operation must also fail closed.
        _replace(
            repository,
            memory_id=memory_id,
            expected_revision=1,
            idempotency_key="key-reused",
            payload_digest="digest-original",
        )
    assert _full_snapshot(db_path) == before

    message = str(different_payload.value)
    assert "Original committed content" not in message
    assert "digest-original" not in message
    assert memory_id not in message


# --------------------------------------------------------------------------
# 1.16 compare-and-set: two replaces, one expected revision, one winner
# --------------------------------------------------------------------------


def test_concurrent_replace_with_same_expected_revision_has_one_winner(
    tmp_path: Path,
) -> None:
    db_path = _authority(tmp_path, "replace-cas")
    memory_id = "memory-cas"
    _add(RecallMCPRepository(db_path), memory_id=memory_id, idempotency_key="key-seed")

    def worker(index: int) -> ReplaceMemoryResult:
        return _replace(
            RecallMCPRepository(db_path),
            memory_id=memory_id,
            expected_revision=1,
            idempotency_key=f"key-replace-{index}",
            payload_digest=f"digest-replace-{index}",
            content=f"Replacement written by racer {index}.",
        )

    results, errors = _run_barrier(worker)

    assert len(results) == 1, results
    assert len(errors) == 1, errors
    winner_index, winner = next(iter(results.items()))
    loser = next(iter(errors.values()))
    assert isinstance(loser, RevisionConflictError)
    assert loser.current_revision == 2
    assert winner.revision == 2

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT content FROM memories WHERE id = ?", (memory_id,)
        ).fetchone() == (f"Replacement written by racer {winner_index}.",)
        assert conn.execute(
            "SELECT revision FROM memory_metadata WHERE memory_id = ?", (memory_id,)
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT revision, operation FROM memory_events WHERE memory_id = ?"
            " ORDER BY revision",
            (memory_id,),
        ).fetchall() == [(1, "add"), (2, "replace")]
        # The loser leaves no idempotency row behind.
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_idempotency WHERE operation = 'replace'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE id = ?", (memory_id,)
        ).fetchone() == (1,)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


# --------------------------------------------------------------------------
# 1.17 idempotency survives an authority restart (separate OS process)
# --------------------------------------------------------------------------


_RESTART_SCRIPT = """
import json, sys
sys.path.insert(0, {src!r})
from recall.mcp_repository import IdempotencyKeyReusedError, RecallMCPRepository

repository = RecallMCPRepository({db!r})
try:
    result = repository.add_memory(
        grant_id={grant!r},
        scope={scope!r},
        memory_id={memory_id!r},
        content=sys.argv[2],
        kind="decision",
        tags=("concurrency",),
        source_conversation="conversation-concurrency",
        actor_id="actor-concurrency",
        idempotency_key={key!r},
        payload_digest=sys.argv[1],
        occurred_at="2026-08-02T06:00:00+00:00",
        embedding_blobs={{1: b"active-generation-vector"}},
    )
except IdempotencyKeyReusedError as exc:
    print(json.dumps({{"error": "IDEMPOTENCY_KEY_REUSED", "message": str(exc)}}))
else:
    print(json.dumps({{
        "memory_id": result.memory_id,
        "revision": result.revision,
        "created_at": result.created_at,
    }}))
"""


def _restart_replay(db_path: Path, memory_id: str, key: str, digest: str, content: str):
    script = _RESTART_SCRIPT.format(
        src=SRC_PATH,
        db=str(db_path),
        grant=WRITE_GRANT_ID,
        scope=SCOPE,
        memory_id=memory_id,
        key=key,
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, digest, content],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip())


def test_idempotency_survives_authority_restart(tmp_path: Path) -> None:
    db_path = _authority(tmp_path, "restart-idempotency")
    memory_id = "memory-restart"
    original = _add(
        RecallMCPRepository(db_path),
        memory_id=memory_id,
        idempotency_key="key-restart",
        payload_digest="digest-restart",
        content="Content committed before the authority restarts.",
    )
    before = _full_snapshot(db_path)

    replayed = _restart_replay(
        db_path,
        memory_id,
        "key-restart",
        "digest-restart",
        "Content committed before the authority restarts.",
    )
    assert replayed == {
        "memory_id": original.memory_id,
        "revision": original.revision,
        "created_at": original.created_at,
    }
    assert _full_snapshot(db_path) == before

    tampered = _restart_replay(
        db_path,
        memory_id,
        "key-restart",
        "digest-restart-tampered",
        "Content that must not survive the restart replay.",
    )
    assert tampered["error"] == "IDEMPOTENCY_KEY_REUSED"
    assert "Content that must not survive" not in tampered["message"]
    assert _full_snapshot(db_path) == before


# --------------------------------------------------------------------------
# 1.17a current authorization is checked before any idempotency lookup
# --------------------------------------------------------------------------


def _install_intruder_owner(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO owners(owner_id, created_at) VALUES (?, ?)",
            (OTHER_OWNER_ID, BOOTSTRAP.created_at),
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
                OTHER_GRANT_ID,
                OTHER_OWNER_ID,
                "https://issuer.invalid",
                "intruder",
                "intruder-client",
                1,
                "OAUTH",
                "kiro",
                # Deliberately maximal: the intruder holds every OAuth scope and
                # the same memory scope string, so any leak would come from a
                # missing owner_id predicate rather than a missing permission.
                '["memory:read","memory:write","memory:admin"]',
                f'["{SCOPE}"]',
                BOOTSTRAP.created_at,
            ),
        )
        # The intruder owns an identically named scope: only owner_id separates
        # the two tenants, which is exactly what the SQL predicates must prove.
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                owner_id, scope, generation, provider,
                endpoint_identity_hash, model, dimension, status,
                created_at, activated_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                OTHER_OWNER_ID,
                SCOPE,
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


def _revoke_grant(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE client_grants SET revoked_at = ? WHERE grant_id = ?",
            ("2026-08-02T05:30:00+00:00", WRITE_GRANT_ID),
        )


def _unlink_grant(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE client_grants SET unlinked_at = ? WHERE grant_id = ?",
            ("2026-08-02T05:30:00+00:00", WRITE_GRANT_ID),
        )


def _downgrade_scope(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE client_grants SET memory_scope_patterns_json = ? WHERE grant_id = ?",
            ('["project:unrelated"]', WRITE_GRANT_ID),
        )


@pytest.mark.parametrize(
    ("name", "mutate", "replay_grant"),
    [
        ("revoked", _revoke_grant, WRITE_GRANT_ID),
        ("unlinked", _unlink_grant, WRITE_GRANT_ID),
        ("scope_downgrade", _downgrade_scope, WRITE_GRANT_ID),
        ("wrong_owner", _install_intruder_owner, OTHER_GRANT_ID),
    ],
)
def test_replay_runs_current_authorization_before_idempotency_lookup(
    tmp_path: Path, name: str, mutate, replay_grant: str
) -> None:
    db_path = _authority(tmp_path, f"authz-{name}")
    repository = RecallMCPRepository(db_path)
    memory_id = f"memory-authz-{name}"
    secret = f"Committed content for the {name} authorization case."
    _add(
        repository,
        memory_id=memory_id,
        idempotency_key="key-authz",
        payload_digest="digest-authz",
        content=secret,
    )
    mutate(db_path)
    before = _full_snapshot(db_path)

    replays = {
        "add": lambda key: _add(
            repository,
            memory_id=memory_id,
            idempotency_key=key,
            payload_digest="digest-authz",
            content=secret,
            grant_id=replay_grant,
        ),
        "replace": lambda key: _replace(
            repository,
            memory_id=memory_id,
            expected_revision=1,
            idempotency_key=key,
            grant_id=replay_grant,
        ),
        "remove": lambda key: _remove(
            repository,
            memory_id=memory_id,
            expected_revision=1,
            idempotency_key=key,
            grant_id=replay_grant,
        ),
    }

    for operation, replay in replays.items():
        with pytest.raises(PermissionError) as known_key:
            replay("key-authz")
        with pytest.raises(PermissionError) as unknown_key:
            replay("key-never-issued")

        # Uniform not-authorized: no content, no key existence signal.
        assert str(known_key.value) == "not authorized", operation
        assert str(known_key.value) == str(unknown_key.value), operation
        assert type(known_key.value) is type(unknown_key.value)
        assert secret not in str(known_key.value)
        assert "key-authz" not in str(known_key.value)
        # Zero side effect.
        assert _full_snapshot(db_path) == before, operation


# --------------------------------------------------------------------------
# 1.19 two owners, three grants: cross-owner/cross-scope access fails closed
# --------------------------------------------------------------------------


OWNER_A_MEMORY = "memory-owner-a"
OWNER_B_MEMORY = "memory-owner-b"
OWNER_A_CONTENT = "Owner A confidential ledger content."
OWNER_B_CONTENT = "Owner B confidential ledger content."


def _seed_two_owners(tmp_path: Path) -> tuple[Path, RecallMCPRepository]:
    db_path = _authority(tmp_path, "cross-owner")
    repository = RecallMCPRepository(db_path)
    _add(
        repository,
        memory_id=OWNER_A_MEMORY,
        idempotency_key="key-owner-a",
        payload_digest="digest-owner-a",
        content=OWNER_A_CONTENT,
    )
    _install_intruder_owner(db_path)
    _add(
        repository,
        memory_id=OWNER_B_MEMORY,
        idempotency_key="key-owner-b",
        payload_digest="digest-owner-b",
        content=OWNER_B_CONTENT,
        grant_id=OTHER_GRANT_ID,
    )
    return db_path, repository


def test_cross_owner_reads_never_cross_the_owner_boundary(tmp_path: Path) -> None:
    db_path, repository = _seed_two_owners(tmp_path)
    before = _full_snapshot(db_path)

    # Three grants: owner A writer, owner A admin, owner B writer.
    for grant_id, own_memory, foreign_memory, own_content in (
        (WRITE_GRANT_ID, OWNER_A_MEMORY, OWNER_B_MEMORY, OWNER_A_CONTENT),
        (ADMIN_GRANT_ID, OWNER_A_MEMORY, OWNER_B_MEMORY, OWNER_A_CONTENT),
        (OTHER_GRANT_ID, OWNER_B_MEMORY, OWNER_A_MEMORY, OWNER_B_CONTENT),
    ):
        own = repository.get_scoped_memory(
            grant_id=grant_id, scope=SCOPE, memory_id=own_memory
        )
        assert own is not None and own.content == own_content

        # Direct id guessing returns nothing at all, not an error that proves
        # the row exists.
        assert (
            repository.get_scoped_memory(
                grant_id=grant_id, scope=SCOPE, memory_id=foreign_memory
            )
            is None
        )
        found = repository.search_scoped(
            grant_id=grant_id, scope=SCOPE, query="confidential"
        )
        assert {result.memory_id for result in found} == {own_memory}
        recent = repository.recent_scoped_memories(grant_id=grant_id, scope=SCOPE)
        assert {result.memory_id for result in recent} == {own_memory}

    assert _full_snapshot(db_path) == before


def test_cross_owner_writes_fail_closed_without_side_effects(tmp_path: Path) -> None:
    db_path, repository = _seed_two_owners(tmp_path)
    before = _full_snapshot(db_path)

    attempts = (
        ("add", lambda: _add(
            repository,
            memory_id=OWNER_A_MEMORY,
            idempotency_key="key-intruder-add",
            payload_digest="digest-intruder",
            content="Intruder content that must never be stored.",
            grant_id=OTHER_GRANT_ID,
        )),
        ("replace", lambda: _replace(
            repository,
            memory_id=OWNER_A_MEMORY,
            expected_revision=1,
            idempotency_key="key-intruder-replace",
            payload_digest="digest-intruder",
            content="Intruder replacement that must never be stored.",
            grant_id=OTHER_GRANT_ID,
        )),
        ("remove", lambda: _remove(
            repository,
            memory_id=OWNER_A_MEMORY,
            expected_revision=1,
            idempotency_key="key-intruder-remove",
            payload_digest="digest-intruder",
            grant_id=OTHER_GRANT_ID,
        )),
        ("restore", lambda: repository.restore_memory(
            grant_id=OTHER_GRANT_ID,
            scope=SCOPE,
            memory_id=OWNER_A_MEMORY,
            expected_revision=1,
            actor_id="actor-intruder",
            idempotency_key="key-intruder-restore",
            payload_digest="digest-intruder",
            occurred_at="2026-08-02T07:00:00+00:00",
        )),
        ("purge", lambda: repository.purge_memory(
            grant_id=OTHER_GRANT_ID,
            scope=SCOPE,
            memory_id=OWNER_A_MEMORY,
            expected_revision=1,
            actor_id="actor-intruder",
            idempotency_key="key-intruder-purge",
            payload_digest="digest-intruder",
            deny_payload_digest="digest-intruder-deny",
            occurred_at="2026-08-02T07:00:00+00:00",
        )),
    )

    for operation, attempt in attempts:
        with pytest.raises(PermissionError) as failure:
            attempt()
        assert str(failure.value) == "not authorized", operation
        assert OWNER_A_CONTENT not in str(failure.value)
        assert _full_snapshot(db_path) == before, operation

    # The owner A writer is equally locked out of owner B's row.
    with pytest.raises(PermissionError):
        _replace(
            repository,
            memory_id=OWNER_B_MEMORY,
            expected_revision=1,
            idempotency_key="key-a-into-b",
            payload_digest="digest-a-into-b",
        )
    assert _full_snapshot(db_path) == before


# --------------------------------------------------------------------------
# 1.20 pure search vs concurrent replace/remove:
#      no lost update, no torn read, no tier/index drift
# --------------------------------------------------------------------------

PURITY_MEMORY = "memory-read-purity"
PURITY_TOKEN = "purityprobe"
PURITY_REPLACES = 12


def _purity_content(revision: int) -> str:
    """Deterministic content per revision, always FTS-matchable on the token."""

    return (
        f"{PURITY_TOKEN} revision {revision} DecisionRecord about "
        f"read-purity under concurrent replace-and-remove pressure."
    )


def _purity_occurred_at(revision: int) -> str:
    return f"2026-08-03T0{revision // 60}:{revision % 60:02d}:00+00:00"


def _seed_purity(db_path: Path) -> None:
    _add(
        RecallMCPRepository(db_path),
        memory_id=PURITY_MEMORY,
        idempotency_key="key-purity-add",
        payload_digest="digest-purity-add",
        content=_purity_content(1),
        occurred_at=_purity_occurred_at(1),
    )


def _purity_writer(db_path: Path, replaces: int = PURITY_REPLACES) -> int:
    """Run ``replaces`` CAS replaces; returns the final revision."""

    revision = 1
    for step in range(replaces):
        next_revision = revision + 1
        _replace(
            RecallMCPRepository(db_path),
            memory_id=PURITY_MEMORY,
            expected_revision=revision,
            idempotency_key=f"key-purity-replace-{step}",
            payload_digest=f"digest-purity-replace-{step}",
            content=_purity_content(next_revision),
            occurred_at=_purity_occurred_at(next_revision),
        )
        revision = next_revision
    return revision


def _purity_reader(db_path: Path, stop: threading.Event) -> list[tuple]:
    """Pure reads only, looping until the writer signals completion."""

    observations: list[tuple] = []
    repository = RecallMCPRepository(db_path)
    while not stop.is_set():
        for result in repository.search_scoped(
            grant_id=WRITE_GRANT_ID, scope=SCOPE, query=PURITY_TOKEN
        ):
            observations.append(
                (
                    "search",
                    result.memory_id,
                    result.revision,
                    result.content,
                    result.content_hash,
                )
            )
        direct = repository.get_scoped_memory(
            grant_id=WRITE_GRANT_ID, scope=SCOPE, memory_id=PURITY_MEMORY
        )
        if direct is not None:
            observations.append(
                (
                    "get",
                    direct.memory_id,
                    direct.revision,
                    direct.content,
                    direct.content_hash,
                )
            )
        for result in repository.recent_scoped_memories(
            grant_id=WRITE_GRANT_ID, scope=SCOPE
        ):
            observations.append(
                (
                    "recent",
                    result.memory_id,
                    result.revision,
                    result.content,
                    result.content_hash,
                )
            )
    return observations


def _run_reader_against_writer(db_path: Path, writer):
    """Run ``writer()`` while a pure-read loop hammers the same authority."""

    stop = threading.Event()
    observations: list[tuple] = []
    reader_error: list[BaseException] = []

    def reader_target() -> None:
        try:
            observations.extend(_purity_reader(db_path, stop))
        except BaseException as exc:  # noqa: BLE001 - surfaced in assertions
            reader_error.append(exc)
            stop.set()

    reader = threading.Thread(target=reader_target)
    reader.start()
    try:
        writer_result = writer()
    finally:
        stop.set()
        reader.join(timeout=90)

    assert not reader.is_alive(), "reader thread deadlocked"
    assert not reader_error, reader_error
    return writer_result, observations


def test_pure_search_never_observes_a_torn_state_and_the_writer_loses_nothing(
    tmp_path: Path,
) -> None:
    db_path = _authority(tmp_path, "read-purity")
    _seed_purity(db_path)

    final_revision, observations = _run_reader_against_writer(
        db_path, lambda: _purity_writer(db_path)
    )

    assert final_revision == PURITY_REPLACES + 1
    assert observations, "the reader never observed the memory"

    seen_revisions = set()
    for kind, memory_id, revision, content, content_hash in observations:
        assert memory_id == PURITY_MEMORY, kind
        # Every observation must be a *committed* state, never a mix of new
        # content with an old revision or a hash that does not match.
        assert 1 <= revision <= final_revision, (kind, revision)
        assert content == _purity_content(revision), (kind, revision)
        assert content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
        seen_revisions.add(revision)

    # Prove the reads really interleaved with the writes rather than all
    # landing before or after the writer ran.
    assert len(seen_revisions) >= 2, sorted(seen_revisions)

    # No lost update: exactly one add + PURITY_REPLACES replaces committed.
    with sqlite3.connect(db_path) as conn:
        revision, updated_at = conn.execute(
            "SELECT revision, updated_at FROM memory_metadata WHERE memory_id = ?",
            (PURITY_MEMORY,),
        ).fetchone()
        events = conn.execute(
            "SELECT revision, operation FROM memory_events "
            "WHERE memory_id = ? ORDER BY revision",
            (PURITY_MEMORY,),
        ).fetchall()
    assert revision == final_revision
    assert updated_at == _purity_occurred_at(final_revision)
    assert [row[0] for row in events] == list(range(1, final_revision + 1))
    assert [row[1] for row in events] == ["add"] + ["replace"] * PURITY_REPLACES


def test_concurrent_pure_search_leaves_the_authority_byte_identical(
    tmp_path: Path,
) -> None:
    """Read purity: the same writer sequence, with and without readers.

    ``mcp_repository`` derives every timestamp from caller-supplied
    ``occurred_at`` values (no clock, no uuid), so two identical write
    sequences must produce identical databases.  Any write performed by the
    "pure" read path -- an access counter, a tier demotion, an FTS rebuild --
    would break the equality.
    """

    quiet_db = _authority(tmp_path, "purity-quiet")
    noisy_db = _authority(tmp_path, "purity-noisy")
    _seed_purity(quiet_db)
    _seed_purity(noisy_db)

    quiet_revision = _purity_writer(quiet_db)
    noisy_revision, observations = _run_reader_against_writer(
        noisy_db, lambda: _purity_writer(noisy_db)
    )

    assert quiet_revision == noisy_revision
    assert observations
    assert _full_snapshot(noisy_db) == _full_snapshot(quiet_db)

    # The legacy hot/cold machinery must not have been touched by reads.
    with sqlite3.connect(noisy_db) as conn:
        tier, access_count, last_accessed_at, last_demoted_at = conn.execute(
            "SELECT tier, access_count, last_accessed_at, last_demoted_at "
            "FROM memories WHERE id = ?",
            (PURITY_MEMORY,),
        ).fetchone()
    assert tier == "hot"
    assert access_count == 0
    assert last_accessed_at is None
    assert last_demoted_at is None


def test_index_parity_holds_after_concurrent_reads_and_writes(tmp_path: Path) -> None:
    db_path = _authority(tmp_path, "purity-index")
    _seed_purity(db_path)
    final_revision, _ = _run_reader_against_writer(
        db_path, lambda: _purity_writer(db_path)
    )
    final_content = _purity_content(final_revision)

    from recall.store import extract_keywords

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT content FROM memories WHERE id = ?", (PURITY_MEMORY,)
        ).fetchone() == (final_content,)

        fts_rows = conn.execute(
            "SELECT content FROM memories_fts WHERE id = ?", (PURITY_MEMORY,)
        ).fetchall()
        assert fts_rows == [(final_content,)], "FTS index drifted from memories"

        keywords = sorted(
            row[0]
            for row in conn.execute(
                "SELECT keyword FROM keywords WHERE memory_id = ?", (PURITY_MEMORY,)
            ).fetchall()
        )
        assert keywords == sorted(
            {keyword.lower() for keyword in extract_keywords(final_content)}
        )

        assert conn.execute(
            "SELECT content_hash FROM memory_metadata WHERE memory_id = ?",
            (PURITY_MEMORY,),
        ).fetchone() == (hashlib.sha256(final_content.encode("utf-8")).hexdigest(),)

        embeddings = conn.execute(
            "SELECT generation, embedding_blob FROM memory_embeddings "
            "WHERE memory_id = ? ORDER BY generation",
            (PURITY_MEMORY,),
        ).fetchall()
        assert embeddings == [(1, b"replaced-generation-vector")]

        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        # FTS5 self-check: raises sqlite3.DatabaseError on index corruption.
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")


def test_search_during_remove_never_returns_a_tombstoned_memory(
    tmp_path: Path,
) -> None:
    db_path = _authority(tmp_path, "purity-remove")
    _seed_purity(db_path)

    def writer() -> int:
        revision = _purity_writer(db_path, replaces=6)
        _remove(
            RecallMCPRepository(db_path),
            memory_id=PURITY_MEMORY,
            expected_revision=revision,
            idempotency_key="key-purity-remove",
            payload_digest="digest-purity-remove",
            occurred_at=_purity_occurred_at(revision + 1),
        )
        return revision + 1

    final_revision, observations = _run_reader_against_writer(db_path, writer)
    assert final_revision == 8

    # Visibility is monotonic: the tombstone revision itself is never visible
    # and the memory never reappears (no stale index resurrection).
    for _kind, _memory_id, revision, content, content_hash in observations:
        assert revision <= final_revision - 1, "a tombstoned revision was returned"
        assert content == _purity_content(revision)
        assert content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()

    repository = RecallMCPRepository(db_path)
    assert (
        repository.search_scoped(
            grant_id=WRITE_GRANT_ID, scope=SCOPE, query=PURITY_TOKEN
        )
        == ()
    )
    assert (
        repository.get_scoped_memory(
            grant_id=WRITE_GRANT_ID, scope=SCOPE, memory_id=PURITY_MEMORY
        )
        is None
    )
    assert (
        repository.recent_scoped_memories(grant_id=WRITE_GRANT_ID, scope=SCOPE) == ()
    )

    # Soft delete stays reversible: the rows survive, only visibility changed.
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id = ?", (PURITY_MEMORY,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE id = ?", (PURITY_MEMORY,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT deleted_at FROM memory_metadata WHERE memory_id = ?",
            (PURITY_MEMORY,),
        ).fetchone() == (_purity_occurred_at(final_revision),)
