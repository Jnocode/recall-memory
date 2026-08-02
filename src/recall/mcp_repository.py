"""MCP-era repository boundary over the canonical Recall SQLite database."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_VECTOR_TABLE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_VEC0_TABLE_SCHEMA = re.compile(
    r"\ACREATE\s+VIRTUAL\s+TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r'"?[A-Za-z_][A-Za-z0-9_]*"?\s+USING\s+vec0\s*\(',
    re.IGNORECASE,
)


def _is_vec0_table_schema(sql: object) -> bool:
    return isinstance(sql, str) and _VEC0_TABLE_SCHEMA.match(sql) is not None


@dataclass(frozen=True)
class ScopedSearchResult:
    memory_id: str
    content: str
    revision: int
    scope: str
    kind: str
    content_hash: str
    source_client: str
    source_conversation: str | None
    actor_id: str
    creator_grant_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ScopedMemoryResult:
    memory_id: str
    content: str
    revision: int
    scope: str
    kind: str
    tags: tuple[str, ...]
    content_hash: str
    source_client: str
    source_conversation: str | None
    actor_id: str
    creator_grant_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AddMemoryResult:
    """Committed result of one transactional MCP memory add."""

    memory_id: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class ReplaceMemoryResult:
    """Committed result of one transactional MCP memory replacement."""

    memory_id: str
    revision: int
    updated_at: str


@dataclass(frozen=True)
class RemoveMemoryResult:
    """Stable result persisted for a successful soft remove."""

    memory_id: str
    revision: int
    deleted_at: str


@dataclass(frozen=True)
class RestoreMemoryResult:
    """Stable result persisted for a successful owner-admin restore."""

    memory_id: str
    revision: int
    restored_at: str


@dataclass(frozen=True)
class PurgeMemoryResult:
    """Content-free result of a committed owner-admin hard purge."""

    memory_id: str
    final_revision: int
    purged_at: str


class PurgedMemoryReplayError(RuntimeError):
    """Raised when an operation's idempotency key matches a purged memory entry."""

    def __init__(self, memory_id: str, idempotency_key: str) -> None:
        super().__init__(f"idempotency key {idempotency_key} for memory {memory_id} was purged")
        self.memory_id = memory_id
        self.idempotency_key = idempotency_key


class IdempotencyKeyReusedError(RuntimeError):
    """The key was already committed with a different payload or operation.

    The message is deliberately content-free: it never echoes the stored
    payload digest, the target memory id, or any memory content.
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__("idempotency key reused with a different payload")
        self.idempotency_key = idempotency_key


class RevisionConflictError(RuntimeError):

    """The caller's expected revision does not match the current row."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("revision conflict")
        self.current_revision = current_revision


class RecallMCPRepository:
    """Repository adapter that never consults the user's default Recall path."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _readonly_connection(self) -> sqlite3.Connection:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _write_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        vector_tables = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND sql IS NOT NULL
            """
        ).fetchall()
        if any(_is_vec0_table_schema(row[0]) for row in vector_tables):
            conn.enable_load_extension(True)
            try:
                import sqlite_vec

                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
        return conn

    def _after_write_stage(self, stage: str, conn: sqlite3.Connection) -> None:
        """Test seam for deterministic transaction-failure injection."""

    @staticmethod
    def _require_text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be blank")
        return value

    @staticmethod
    def _stable_id(prefix: str, *parts: object) -> str:
        digest = hashlib.sha256()
        for part in parts:
            encoded = str(part).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return f"{prefix}-{digest.hexdigest()[:24]}"

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _resolve_idempotency(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str,
        grant_id: str,
        operation: str,
        idempotency_key: str,
        payload_digest: str,
    ) -> dict[str, object] | None:
        """Return the replayable result for a key, or fail closed.

        Callers MUST have already validated the current grant, so reaching this
        method already proves the caller is authorized right now.  The lookup is
        intentionally not restricted to ``operation`` so that a purged deny
        tombstone written by any operation blocks every replay of the same key.

        Returns ``None`` when the key has never been committed.
        """

        row = conn.execute(
            """
            SELECT result_json, purged_at, memory_id, operation, payload_digest
            FROM mcp_idempotency
            WHERE owner_id = ? AND grant_id = ? AND idempotency_key = ?
            """,
            (owner_id, grant_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row[1] is not None:
            raise PurgedMemoryReplayError(
                memory_id=row[2], idempotency_key=idempotency_key
            )
        if row[3] != operation or row[4] != payload_digest:
            raise IdempotencyKeyReusedError(idempotency_key)
        if row[0] is None:
            # An active row without a result cannot be replayed safely.
            raise IdempotencyKeyReusedError(idempotency_key)
        return json.loads(row[0])

    def add_memory(
        self,
        *,
        grant_id: str,
        scope: str,
        memory_id: str,
        content: str,
        kind: str,
        tags: tuple[str, ...] = (),
        source_conversation: str | None,
        actor_id: str,
        idempotency_key: str,
        payload_digest: str,
        occurred_at: str,
        embedding_blobs: dict[int, bytes],
    ) -> AddMemoryResult:
        """Atomically add a memory and every derived/audit row.

        Identity and source client are always resolved from the current grant;
        callers cannot supply an owner or source-client claim.  The service
        layer is responsible for producing the keyed ``payload_digest``.
        """

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("memory_id", memory_id),
            ("content", content),
            ("kind", kind),
            ("actor_id", actor_id),
            ("idempotency_key", idempotency_key),
            ("payload_digest", payload_digest),
            ("occurred_at", occurred_at),
        ):
            self._require_text(name, value)
        if not isinstance(tags, tuple) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags
        ):
            raise ValueError("tags must be a tuple of nonblank strings")
        if source_conversation is not None and (
            not isinstance(source_conversation, str) or not source_conversation.strip()
        ):
            raise ValueError("source_conversation must be null or nonblank text")
        if not isinstance(embedding_blobs, dict) or not embedding_blobs:
            raise ValueError("embedding_blobs must contain at least one generation")
        if any(
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(blob, bytes)
            or not blob
            for generation, blob in embedding_blobs.items()
        ):
            raise ValueError("embedding_blobs must map positive generations to bytes")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT owner_id, source_client
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text'
                        AND value IN ('memory:write', 'memory:admin')
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id, source_client = grant

            replay = self._resolve_idempotency(
                conn,
                owner_id=owner_id,
                grant_id=grant_id,
                operation="add",
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )
            if replay is not None:
                return AddMemoryResult(
                    memory_id=replay["memory_id"],
                    revision=replay["revision"],
                    created_at=replay["created_at"],
                )

            # Fail closed on any id that already exists: a caller must never be
            # able to probe or collide with another owner's memory id, and a
            # same-owner collision is never a legitimate add.
            if conn.execute(
                "SELECT 1 FROM memory_metadata WHERE memory_id = ?",
                (memory_id,),
            ).fetchone() is not None:
                raise PermissionError("not authorized")

            profiles = conn.execute(
                """
                SELECT generation, provider, endpoint_identity_hash, model, dimension
                FROM embedding_profiles
                WHERE owner_id = ? AND scope = ?
                  AND status IN ('ACTIVE', 'BUILDING')
                ORDER BY generation
                """,
                (owner_id, scope),
            ).fetchall()
            profile_generations = {int(row[0]) for row in profiles}
            if profile_generations != set(embedding_blobs):
                raise ValueError(
                    "embedding_blobs must exactly cover ACTIVE and BUILDING generations"
                )

            from .store import extract_keywords

            tags_json = self._canonical_json(list(tags))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            result = AddMemoryResult(memory_id=memory_id, revision=1, created_at=occurred_at)
            result_json = self._canonical_json(
                {
                    "created_at": result.created_at,
                    "memory_id": result.memory_id,
                    "revision": result.revision,
                }
            )

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
                    occurred_at,
                    None,
                    0,
                    source_conversation or "",
                    kind,
                    "hot",
                    None,
                    None,
                ),
            )
            self._after_write_stage("memory", conn)

            normalized_keywords = sorted(
                {keyword.lower() for keyword in extract_keywords(content)}
            )
            for keyword in normalized_keywords:
                conn.execute(
                    "INSERT INTO keywords(keyword, memory_id) VALUES (?, ?)",
                    (keyword, memory_id),
                )
            self._after_write_stage("keywords", conn)

            conn.execute(
                "INSERT INTO memories_fts(content, id) VALUES (?, ?)",
                (content, memory_id),
            )
            self._after_write_stage("fts", conn)

            provenance_json = self._canonical_json(
                [
                    {
                        "dimension": int(row[4]),
                        "endpoint_identity_hash": row[2],
                        "generation": int(row[0]),
                        "model": row[3],
                        "provider": row[1],
                    }
                    for row in profiles
                ]
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
                    owner_id,
                    grant_id,
                    1,
                    scope,
                    kind,
                    tags_json,
                    content_hash,
                    source_client,
                    source_conversation,
                    actor_id,
                    occurred_at,
                    occurred_at,
                    None,
                    0,
                    provenance_json,
                ),
            )
            self._after_write_stage("metadata", conn)

            for generation, provider, endpoint_hash, model, dimension in profiles:
                conn.execute(
                    """
                    INSERT INTO memory_embeddings (
                        embedding_id, memory_id, owner_id, scope, generation,
                        provider, endpoint_identity_hash, model, dimension,
                        embedding_blob, vector_rowid, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._stable_id("embedding", memory_id, generation),
                        memory_id,
                        owner_id,
                        scope,
                        generation,
                        provider,
                        endpoint_hash,
                        model,
                        dimension,
                        embedding_blobs[int(generation)],
                        None,
                        occurred_at,
                    ),
                )
            self._after_write_stage("embeddings", conn)

            conn.execute(
                """
                INSERT INTO memory_events (
                    event_id, memory_id, owner_id, grant_id, revision, operation,
                    actor_id, source_client, occurred_at, payload_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._stable_id("event", memory_id, 1, "add"),
                    memory_id,
                    owner_id,
                    grant_id,
                    1,
                    "add",
                    actor_id,
                    source_client,
                    occurred_at,
                    payload_digest,
                ),
            )
            self._after_write_stage("event", conn)

            conn.execute(
                """
                INSERT INTO mcp_idempotency (
                    owner_id, grant_id, actor_id, operation, idempotency_key,
                    memory_id, scope, result_schema_version, payload_digest,
                    result_json, created_at, purged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    grant_id,
                    actor_id,
                    "add",
                    idempotency_key,
                    memory_id,
                    scope,
                    1,
                    payload_digest,
                    result_json,
                    occurred_at,
                    None,
                ),
            )
            self._after_write_stage("idempotency", conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace_memory(
        self,
        *,
        grant_id: str,
        scope: str,
        memory_id: str,
        expected_revision: int,
        content: str,
        kind: str,
        tags: tuple[str, ...] = (),
        source_conversation: str | None,
        actor_id: str,
        idempotency_key: str,
        payload_digest: str,
        occurred_at: str,
        embedding_blobs: dict[int, bytes],
        new_scope: str | None = None,
    ) -> ReplaceMemoryResult:
        """Atomically replace a visible memory and rebuild every derived row."""

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("memory_id", memory_id),
            ("content", content),
            ("kind", kind),
            ("actor_id", actor_id),
            ("idempotency_key", idempotency_key),
            ("payload_digest", payload_digest),
            ("occurred_at", occurred_at),
        ):
            self._require_text(name, value)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        if not isinstance(tags, tuple) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags
        ):
            raise ValueError("tags must be a tuple of nonblank strings")
        if source_conversation is not None and (
            not isinstance(source_conversation, str) or not source_conversation.strip()
        ):
            raise ValueError("source_conversation must be null or nonblank text")
        if new_scope is not None:
            self._require_text("new_scope", new_scope)
        target_scope = new_scope if new_scope is not None else scope
        if not isinstance(embedding_blobs, dict) or not embedding_blobs:
            raise ValueError("embedding_blobs must contain at least one generation")
        if any(
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(blob, bytes)
            or not blob
            for generation, blob in embedding_blobs.items()
        ):
            raise ValueError("embedding_blobs must map positive generations to bytes")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if target_scope != scope:
                conn.execute("PRAGMA defer_foreign_keys=ON")
            grant = conn.execute(
                """
                SELECT owner_id, source_client
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text'
                        AND value IN ('memory:write', 'memory:admin')
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope, target_scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id, source_client = grant

            replay = self._resolve_idempotency(
                conn,
                owner_id=owner_id,
                grant_id=grant_id,
                operation="replace",
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )
            if replay is not None:
                return ReplaceMemoryResult(
                    memory_id=replay["memory_id"],
                    revision=replay["revision"],
                    updated_at=replay["updated_at"],
                )

            current = conn.execute(
                """
                SELECT revision
                FROM memory_metadata
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND deleted_at IS NULL
                """,
                (memory_id, owner_id, scope),
            ).fetchone()
            if current is None:
                raise PermissionError("not authorized")
            current_revision = int(current[0])
            if current_revision != expected_revision:
                raise RevisionConflictError(current_revision)

            profiles = conn.execute(
                """
                SELECT generation, provider, endpoint_identity_hash, model, dimension
                FROM embedding_profiles
                WHERE owner_id = ? AND scope = ?
                  AND status IN ('ACTIVE', 'BUILDING')
                ORDER BY generation
                """,
                (owner_id, target_scope),
            ).fetchall()
            if target_scope != scope:
                source_profiles = conn.execute(
                    """
                    SELECT generation, provider, endpoint_identity_hash, model, dimension
                    FROM embedding_profiles
                    WHERE owner_id = ? AND scope = ?
                      AND status IN ('ACTIVE', 'BUILDING')
                    ORDER BY generation
                    """,
                    (owner_id, scope),
                ).fetchall()
                if source_profiles != profiles:
                    raise ValueError(
                        "source and destination embedding profiles must match"
                    )
                projected = conn.execute(
                    """
                    SELECT 1 FROM memory_embeddings
                    WHERE memory_id = ? AND owner_id = ? AND scope = ?
                      AND vector_rowid IS NOT NULL
                    LIMIT 1
                    """,
                    (memory_id, owner_id, scope),
                ).fetchone()
                if projected is not None:
                    raise RuntimeError(
                        "scope move with physical vectors is not yet supported"
                    )
            if {int(row[0]) for row in profiles} != set(embedding_blobs):
                raise ValueError(
                    "embedding_blobs must exactly cover ACTIVE and BUILDING generations"
                )

            from .store import extract_keywords

            next_revision = current_revision + 1
            tags_json = self._canonical_json(list(tags))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            provenance_json = self._canonical_json(
                [
                    {
                        "dimension": int(row[4]),
                        "endpoint_identity_hash": row[2],
                        "generation": int(row[0]),
                        "model": row[3],
                        "provider": row[1],
                    }
                    for row in profiles
                ]
            )
            result = ReplaceMemoryResult(
                memory_id=memory_id,
                revision=next_revision,
                updated_at=occurred_at,
            )
            result_json = self._canonical_json(
                {
                    "memory_id": result.memory_id,
                    "revision": result.revision,
                    "updated_at": result.updated_at,
                }
            )

            updated = conn.execute(
                """
                UPDATE memories
                SET content = ?, session_id = ?, tag = ?
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = memories.id
                        AND owner_id = ? AND scope = ? AND revision = ?
                        AND deleted_at IS NULL
                  )
                """,
                (
                    content,
                    source_conversation or "",
                    kind,
                    memory_id,
                    owner_id,
                    scope,
                    current_revision,
                ),
            )
            if updated.rowcount != 1:
                raise RevisionConflictError(current_revision)
            self._after_write_stage("memory", conn)

            conn.execute(
                """
                DELETE FROM keywords
                WHERE memory_id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ? AND deleted_at IS NULL
                  )
                """,
                (memory_id, memory_id, owner_id, scope, current_revision),
            )
            normalized_keywords = sorted(
                {keyword.lower() for keyword in extract_keywords(content)}
            )
            for keyword in normalized_keywords:
                conn.execute(
                    "INSERT INTO keywords(keyword, memory_id) VALUES (?, ?)",
                    (keyword, memory_id),
                )
            self._after_write_stage("keywords", conn)

            conn.execute(
                """
                DELETE FROM memories_fts
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ? AND deleted_at IS NULL
                  )
                """,
                (memory_id, memory_id, owner_id, scope, current_revision),
            )
            conn.execute(
                "INSERT INTO memories_fts(content, id) VALUES (?, ?)",
                (content, memory_id),
            )
            self._after_write_stage("fts", conn)

            metadata = conn.execute(
                """
                UPDATE memory_metadata
                SET revision = ?, scope = ?, kind = ?, tags_json = ?,
                    content_hash = ?, source_client = ?, source_conversation = ?,
                    actor_id = ?, updated_at = ?, reindex_required = 0,
                    embedding_provenance_json = ?
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND revision = ? AND deleted_at IS NULL
                """,
                (
                    next_revision,
                    target_scope,
                    kind,
                    tags_json,
                    content_hash,
                    source_client,
                    source_conversation,
                    actor_id,
                    occurred_at,
                    provenance_json,
                    memory_id,
                    owner_id,
                    scope,
                    current_revision,
                ),
            )
            if metadata.rowcount != 1:
                raise RevisionConflictError(current_revision)
            self._after_write_stage("metadata", conn)

            for generation, provider, endpoint_hash, model, dimension in profiles:
                embedding = conn.execute(
                    """
                    UPDATE memory_embeddings
                    SET scope = ?, provider = ?, endpoint_identity_hash = ?,
                        model = ?, dimension = ?, embedding_blob = ?,
                        vector_rowid = NULL, created_at = ?
                    WHERE memory_id = ? AND owner_id = ? AND scope = ?
                      AND generation = ?
                    """,
                    (
                        target_scope,
                        provider,
                        endpoint_hash,
                        model,
                        dimension,
                        embedding_blobs[int(generation)],
                        occurred_at,
                        memory_id,
                        owner_id,
                        scope,
                        generation,
                    ),
                )
                if embedding.rowcount > 1:
                    raise RuntimeError("embedding index parity mismatch")
                if embedding.rowcount == 0:
                    # A BUILDING generation created after this memory has no
                    # mapping yet; dual-write must backfill it in the same
                    # transaction instead of failing the replace.
                    conn.execute(
                        """
                        INSERT INTO memory_embeddings (
                            embedding_id, memory_id, owner_id, scope, generation,
                            provider, endpoint_identity_hash, model, dimension,
                            embedding_blob, vector_rowid, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._stable_id("embedding", memory_id, generation),
                            memory_id,
                            owner_id,
                            target_scope,
                            generation,
                            provider,
                            endpoint_hash,
                            model,
                            dimension,
                            embedding_blobs[int(generation)],
                            None,
                            occurred_at,
                        ),
                    )
            self._after_write_stage("embeddings", conn)

            conn.execute(
                """
                INSERT INTO memory_events (
                    event_id, memory_id, owner_id, grant_id, revision, operation,
                    actor_id, source_client, occurred_at, payload_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._stable_id("event", memory_id, next_revision, "replace"),
                    memory_id,
                    owner_id,
                    grant_id,
                    next_revision,
                    "replace",
                    actor_id,
                    source_client,
                    occurred_at,
                    payload_digest,
                ),
            )
            self._after_write_stage("event", conn)

            conn.execute(
                """
                INSERT INTO mcp_idempotency (
                    owner_id, grant_id, actor_id, operation, idempotency_key,
                    memory_id, scope, result_schema_version, payload_digest,
                    result_json, created_at, purged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    grant_id,
                    actor_id,
                    "replace",
                    idempotency_key,
                    memory_id,
                    target_scope,
                    1,
                    payload_digest,
                    result_json,
                    occurred_at,
                    None,
                ),
            )
            self._after_write_stage("idempotency", conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cutover_embedding_generation(
        self,
        *,
        grant_id: str,
        scope: str,
        expected_active_generation: int,
        building_generation: int,
        activated_at: str,
    ) -> None:
        """Atomically retire the expected ACTIVE generation and activate BUILDING."""

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("activated_at", activated_at),
        ):
            self._require_text(name, value)
        for name, value in (
            ("expected_active_generation", expected_active_generation),
            ("building_generation", building_generation),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if expected_active_generation == building_generation:
            raise ValueError("ACTIVE and BUILDING generations must differ")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT owner_id
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text' AND value = 'memory:admin'
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id = str(grant[0])

            active = conn.execute(
                """
                SELECT generation FROM embedding_profiles
                WHERE owner_id = ? AND scope = ? AND status = 'ACTIVE'
                """,
                (owner_id, scope),
            ).fetchall()
            building = conn.execute(
                """
                SELECT provider, endpoint_identity_hash, model, dimension
                FROM embedding_profiles
                WHERE owner_id = ? AND scope = ? AND generation = ?
                  AND status = 'BUILDING'
                """,
                (owner_id, scope, building_generation),
            ).fetchone()
            if active != [(expected_active_generation,)] or building is None:
                raise PermissionError("not authorized")

            canonical_ids = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT memory_id FROM memory_metadata
                    WHERE owner_id = ? AND scope = ?
                    """,
                    (owner_id, scope),
                ).fetchall()
            }
            active_ids = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT memory_id FROM memory_embeddings
                    WHERE owner_id = ? AND scope = ? AND generation = ?
                    """,
                    (owner_id, scope, expected_active_generation),
                ).fetchall()
            }
            building_rows = conn.execute(
                """
                SELECT memory_id, provider, endpoint_identity_hash, model,
                       dimension, vector_rowid
                FROM memory_embeddings
                WHERE owner_id = ? AND scope = ? AND generation = ?
                """,
                (owner_id, scope, building_generation),
            ).fetchall()
            building_ids = {str(row[0]) for row in building_rows}
            if canonical_ids != active_ids or active_ids != building_ids:
                raise RuntimeError("embedding generation parity mismatch")
            expected_provenance = tuple(building)
            if any(tuple(row[1:5]) != expected_provenance for row in building_rows):
                raise RuntimeError("BUILDING embedding provenance mismatch")

            registry = conn.execute(
                """
                SELECT table_name FROM vector_tables
                WHERE owner_id = ? AND scope = ? AND generation = ?
                """,
                (owner_id, scope, building_generation),
            ).fetchone()
            if registry is None:
                if any(row[5] is not None for row in building_rows):
                    raise RuntimeError("BUILDING vectors have no registered vector table")
            else:
                table_name = registry[0]
                if not isinstance(table_name, str) or _VECTOR_TABLE_NAME.fullmatch(
                    table_name
                ) is None:
                    raise RuntimeError("registered generation vector table is invalid")
                registered_table = conn.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()
                table_kind = conn.execute(
                    """
                    SELECT type FROM pragma_table_list
                    WHERE schema = 'main' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()
                if (
                    registered_table is None
                    or table_kind is None
                    or table_kind[0] != "virtual"
                    or not _is_vec0_table_schema(registered_table[0])
                    or any(row[5] is None for row in building_rows)
                ):
                    raise RuntimeError("registered generation vector table is invalid")
                quoted_table_name = table_name.replace('"', '""')
                for row in building_rows:
                    physical = conn.execute(
                        f'SELECT 1 FROM "{quoted_table_name}" WHERE rowid = ?',
                        (int(row[5]),),
                    ).fetchone()
                    if physical is None:
                        raise RuntimeError("BUILDING physical vector parity mismatch")

            retired = conn.execute(
                """
                UPDATE embedding_profiles
                SET status = 'RETIRED', retired_at = ?
                WHERE owner_id = ? AND scope = ? AND generation = ?
                  AND status = 'ACTIVE'
                """,
                (activated_at, owner_id, scope, expected_active_generation),
            )
            if retired.rowcount != 1:
                raise RuntimeError("ACTIVE generation changed during cutover")
            self._after_write_stage("cutover_retired_active", conn)

            activated = conn.execute(
                """
                UPDATE embedding_profiles
                SET status = 'ACTIVE', activated_at = ?, retired_at = NULL
                WHERE owner_id = ? AND scope = ? AND generation = ?
                  AND status = 'BUILDING'
                """,
                (activated_at, owner_id, scope, building_generation),
            )
            if activated.rowcount != 1:
                raise RuntimeError("BUILDING generation changed during cutover")
            self._after_write_stage("cutover_activated_building", conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_embedding_generation(
        self,
        *,
        grant_id: str,
        scope: str,
        generation: int,
        failed_at: str,
    ) -> None:
        """Fail and clean one BUILDING generation without touching ACTIVE vectors."""

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("failed_at", failed_at),
        ):
            self._require_text(name, value)
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ValueError("generation must be a positive integer")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT owner_id
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text' AND value = 'memory:admin'
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id = str(grant[0])

            active_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM embedding_profiles
                    WHERE owner_id = ? AND scope = ? AND status = 'ACTIVE'
                    """,
                    (owner_id, scope),
                ).fetchone()[0]
            )
            building = conn.execute(
                """
                SELECT 1 FROM embedding_profiles
                WHERE owner_id = ? AND scope = ? AND generation = ?
                  AND status = 'BUILDING'
                """,
                (owner_id, scope, generation),
            ).fetchone()
            if active_count != 1 or building is None:
                raise PermissionError("not authorized")

            registry = conn.execute(
                """
                SELECT table_name FROM vector_tables
                WHERE owner_id = ? AND scope = ? AND generation = ?
                """,
                (owner_id, scope, generation),
            ).fetchone()
            mapped_rows = conn.execute(
                """
                SELECT vector_rowid FROM memory_embeddings
                WHERE owner_id = ? AND scope = ? AND generation = ?
                """,
                (owner_id, scope, generation),
            ).fetchall()
            if registry is None and any(row[0] is not None for row in mapped_rows):
                raise RuntimeError("BUILDING vectors have no registered vector table")

            if registry is not None:
                table_name = registry[0]
                if not isinstance(table_name, str) or _VECTOR_TABLE_NAME.fullmatch(
                    table_name
                ) is None:
                    raise RuntimeError("registered generation vector table is invalid")
                registered_table = conn.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()
                table_kind = conn.execute(
                    """
                    SELECT type FROM pragma_table_list
                    WHERE schema = 'main' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()
                if (
                    registered_table is None
                    or table_kind is None
                    or table_kind[0] != "virtual"
                    or not _is_vec0_table_schema(registered_table[0])
                ):
                    raise RuntimeError("registered generation vector table is invalid")
                quoted_table_name = table_name.replace('"', '""')
                conn.execute(f'DROP TABLE "{quoted_table_name}"')
            self._after_write_stage("failed_generation_vector_table", conn)

            conn.execute(
                """
                DELETE FROM memory_embeddings
                WHERE owner_id = ? AND scope = ? AND generation = ?
                """,
                (owner_id, scope, generation),
            )
            self._after_write_stage("failed_generation_embeddings", conn)

            conn.execute(
                """
                DELETE FROM vector_tables
                WHERE owner_id = ? AND scope = ? AND generation = ?
                """,
                (owner_id, scope, generation),
            )
            self._after_write_stage("failed_generation_registry", conn)

            failed = conn.execute(
                """
                UPDATE embedding_profiles
                SET status = 'FAILED', retired_at = ?
                WHERE owner_id = ? AND scope = ? AND generation = ?
                  AND status = 'BUILDING'
                """,
                (failed_at, owner_id, scope, generation),
            )
            if failed.rowcount != 1:
                raise RuntimeError("BUILDING generation changed during cleanup")
            self._after_write_stage("failed_generation_profile", conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def remove_memory(
        self,
        *,
        grant_id: str,
        scope: str,
        memory_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        payload_digest: str,
        occurred_at: str,
    ) -> RemoveMemoryResult:
        """Atomically tombstone a visible memory while preserving reversible rows."""

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("memory_id", memory_id),
            ("actor_id", actor_id),
            ("idempotency_key", idempotency_key),
            ("payload_digest", payload_digest),
            ("occurred_at", occurred_at),
        ):
            self._require_text(name, value)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT owner_id, source_client
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text'
                        AND value IN ('memory:write', 'memory:admin')
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id, source_client = grant

            replay = self._resolve_idempotency(
                conn,
                owner_id=owner_id,
                grant_id=grant_id,
                operation="remove",
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )
            if replay is not None:
                return RemoveMemoryResult(
                    memory_id=replay["memory_id"],
                    revision=replay["revision"],
                    deleted_at=replay["deleted_at"],
                )

            current = conn.execute(
                """
                SELECT revision
                FROM memory_metadata
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND deleted_at IS NULL
                """,
                (memory_id, owner_id, scope),
            ).fetchone()
            if current is None:
                raise PermissionError("not authorized")
            current_revision = int(current[0])
            if current_revision != expected_revision:
                raise RevisionConflictError(current_revision)

            next_revision = current_revision + 1
            result = RemoveMemoryResult(
                memory_id=memory_id,
                revision=next_revision,
                deleted_at=occurred_at,
            )
            result_json = self._canonical_json(
                {
                    "deleted_at": result.deleted_at,
                    "memory_id": result.memory_id,
                    "revision": result.revision,
                }
            )

            metadata = conn.execute(
                """
                UPDATE memory_metadata
                SET revision = ?, source_client = ?, actor_id = ?,
                    updated_at = ?, deleted_at = ?
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND revision = ? AND deleted_at IS NULL
                """,
                (
                    next_revision,
                    source_client,
                    actor_id,
                    occurred_at,
                    occurred_at,
                    memory_id,
                    owner_id,
                    scope,
                    current_revision,
                ),
            )
            if metadata.rowcount != 1:
                raise RevisionConflictError(current_revision)
            self._after_write_stage("metadata", conn)

            conn.execute(
                """
                INSERT INTO memory_events (
                    event_id, memory_id, owner_id, grant_id, revision, operation,
                    actor_id, source_client, occurred_at, payload_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._stable_id("event", memory_id, next_revision, "remove"),
                    memory_id,
                    owner_id,
                    grant_id,
                    next_revision,
                    "remove",
                    actor_id,
                    source_client,
                    occurred_at,
                    payload_digest,
                ),
            )
            self._after_write_stage("event", conn)

            conn.execute(
                """
                INSERT INTO mcp_idempotency (
                    owner_id, grant_id, actor_id, operation, idempotency_key,
                    memory_id, scope, result_schema_version, payload_digest,
                    result_json, created_at, purged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    grant_id,
                    actor_id,
                    "remove",
                    idempotency_key,
                    memory_id,
                    scope,
                    1,
                    payload_digest,
                    result_json,
                    occurred_at,
                    None,
                ),
            )
            self._after_write_stage("idempotency", conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def restore_memory(
        self,
        *,
        grant_id: str,
        scope: str,
        memory_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        payload_digest: str,
        occurred_at: str,
    ) -> RestoreMemoryResult:
        """Restore one tombstone through an exact-scope owner-admin transaction."""

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("memory_id", memory_id),
            ("actor_id", actor_id),
            ("idempotency_key", idempotency_key),
            ("payload_digest", payload_digest),
            ("occurred_at", occurred_at),
        ):
            self._require_text(name, value)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT owner_id, source_client
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text' AND value = 'memory:admin'
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id, source_client = grant

            replay = self._resolve_idempotency(
                conn,
                owner_id=owner_id,
                grant_id=grant_id,
                operation="restore",
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )
            if replay is not None:
                return RestoreMemoryResult(
                    memory_id=replay["memory_id"],
                    revision=replay["revision"],
                    restored_at=replay["restored_at"],
                )

            current = conn.execute(
                """
                SELECT mm.revision, m.content
                FROM memory_metadata AS mm
                JOIN memories AS m ON m.id = mm.memory_id
                WHERE mm.memory_id = ? AND mm.owner_id = ? AND mm.scope = ?
                  AND mm.deleted_at IS NOT NULL
                """,
                (memory_id, owner_id, scope),
            ).fetchone()
            if current is None:
                raise PermissionError("not authorized")
            current_revision, content = int(current[0]), str(current[1])
            if current_revision != expected_revision:
                raise RevisionConflictError(current_revision)

            profile_generations = {
                int(row[0])
                for row in conn.execute(
                    """
                    SELECT generation
                    FROM embedding_profiles
                    WHERE owner_id = ? AND scope = ?
                      AND status IN ('ACTIVE', 'BUILDING')
                    """,
                    (owner_id, scope),
                )
            }
            embedding_generations = {
                int(row[0])
                for row in conn.execute(
                    """
                    SELECT generation
                    FROM memory_embeddings
                    WHERE memory_id = ? AND owner_id = ? AND scope = ?
                    """,
                    (memory_id, owner_id, scope),
                )
            }
            if not profile_generations or not profile_generations.issubset(
                embedding_generations
            ):
                raise ValueError("restore requires complete ACTIVE/BUILDING embeddings")

            next_revision = current_revision + 1
            from .store import extract_keywords

            result = RestoreMemoryResult(
                memory_id=memory_id,
                revision=next_revision,
                restored_at=occurred_at,
            )
            result_json = self._canonical_json(
                {
                    "memory_id": result.memory_id,
                    "restored_at": result.restored_at,
                    "revision": result.revision,
                }
            )

            conn.execute(
                """
                DELETE FROM keywords
                WHERE memory_id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ? AND deleted_at IS NOT NULL
                  )
                """,
                (memory_id, memory_id, owner_id, scope, current_revision),
            )
            for keyword in sorted(
                {keyword.lower() for keyword in extract_keywords(content)}
            ):
                conn.execute(
                    "INSERT INTO keywords(keyword, memory_id) VALUES (?, ?)",
                    (keyword, memory_id),
                )
            self._after_write_stage("keywords", conn)

            conn.execute(
                """
                DELETE FROM memories_fts
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ? AND deleted_at IS NOT NULL
                  )
                """,
                (memory_id, memory_id, owner_id, scope, current_revision),
            )
            conn.execute(
                "INSERT INTO memories_fts(content, id) VALUES (?, ?)",
                (content, memory_id),
            )
            self._after_write_stage("fts", conn)

            metadata = conn.execute(
                """
                UPDATE memory_metadata
                SET revision = ?, source_client = ?, actor_id = ?,
                    updated_at = ?, deleted_at = NULL
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND revision = ? AND deleted_at IS NOT NULL
                """,
                (
                    next_revision,
                    source_client,
                    actor_id,
                    occurred_at,
                    memory_id,
                    owner_id,
                    scope,
                    current_revision,
                ),
            )
            if metadata.rowcount != 1:
                raise RevisionConflictError(current_revision)
            self._after_write_stage("metadata", conn)

            conn.execute(
                """
                INSERT INTO memory_events (
                    event_id, memory_id, owner_id, grant_id, revision, operation,
                    actor_id, source_client, occurred_at, payload_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._stable_id("event", memory_id, next_revision, "restore"),
                    memory_id,
                    owner_id,
                    grant_id,
                    next_revision,
                    "restore",
                    actor_id,
                    source_client,
                    occurred_at,
                    payload_digest,
                ),
            )
            self._after_write_stage("event", conn)

            conn.execute(
                """
                INSERT INTO mcp_idempotency (
                    owner_id, grant_id, actor_id, operation, idempotency_key,
                    memory_id, scope, result_schema_version, payload_digest,
                    result_json, created_at, purged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    grant_id,
                    actor_id,
                    "restore",
                    idempotency_key,
                    memory_id,
                    scope,
                    1,
                    payload_digest,
                    result_json,
                    occurred_at,
                    None,
                ),
            )
            self._after_write_stage("idempotency", conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def purge_memory(
        self,
        *,
        grant_id: str,
        scope: str,
        memory_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        payload_digest: str,
        deny_payload_digest: str,
        occurred_at: str,
    ) -> PurgeMemoryResult:
        """Hard-purge one authorized memory while retaining content-free audit rows."""

        for name, value in (
            ("grant_id", grant_id),
            ("scope", scope),
            ("memory_id", memory_id),
            ("actor_id", actor_id),
            ("idempotency_key", idempotency_key),
            ("payload_digest", payload_digest),
            ("deny_payload_digest", deny_payload_digest),
            ("occurred_at", occurred_at),
        ):
            self._require_text(name, value)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")

        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT owner_id, source_client
                FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text' AND value = 'memory:admin'
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(memory_scope_patterns_json)
                      WHERE type = 'text' AND value = ?
                  )
                """,
                (grant_id, scope),
            ).fetchone()
            if grant is None:
                raise PermissionError("not authorized")
            owner_id, source_client = grant

            current = conn.execute(
                """
                SELECT revision
                FROM memory_metadata
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                """,
                (memory_id, owner_id, scope),
            ).fetchone()
            if current is None:
                raise PermissionError("not authorized")
            current_revision = int(current[0])
            if current_revision != expected_revision:
                raise RevisionConflictError(current_revision)

            final_revision = current_revision + 1
            result = PurgeMemoryResult(
                memory_id=memory_id,
                final_revision=final_revision,
                purged_at=occurred_at,
            )

            conn.execute(
                """
                INSERT INTO memory_events (
                    event_id, memory_id, owner_id, grant_id, revision, operation,
                    actor_id, source_client, occurred_at, payload_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._stable_id("event", memory_id, final_revision, "purge"),
                    memory_id,
                    owner_id,
                    grant_id,
                    final_revision,
                    "purge",
                    actor_id,
                    source_client,
                    occurred_at,
                    payload_digest,
                ),
            )
            self._after_write_stage("purge_event", conn)

            conn.execute(
                """
                INSERT INTO mcp_idempotency (
                    owner_id, grant_id, actor_id, operation, idempotency_key,
                    memory_id, scope, result_schema_version, payload_digest,
                    result_json, created_at, purged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    owner_id,
                    grant_id,
                    actor_id,
                    "purge",
                    idempotency_key,
                    memory_id,
                    scope,
                    1,
                    deny_payload_digest,
                    occurred_at,
                    occurred_at,
                ),
            )
            scrubbed = conn.execute(
                """
                UPDATE mcp_idempotency
                SET result_json = NULL, payload_digest = ?, purged_at = ?
                WHERE owner_id = ? AND memory_id = ?
                """,
                (deny_payload_digest, occurred_at, owner_id, memory_id),
            )
            if scrubbed.rowcount < 1:
                raise RuntimeError("purge idempotency scrub did not retain a deny row")
            self._after_write_stage("purge_idempotency", conn)

            generation_vectors = conn.execute(
                """
                SELECT vt.table_name, me.vector_rowid
                FROM memory_embeddings AS me
                JOIN vector_tables AS vt
                  ON vt.owner_id = me.owner_id
                 AND vt.scope = me.scope
                 AND vt.generation = me.generation
                WHERE me.memory_id = ? AND me.owner_id = ? AND me.scope = ?
                ORDER BY me.generation
                """,
                (memory_id, owner_id, scope),
            ).fetchall()
            for table_name, vector_rowid in generation_vectors:
                if not isinstance(table_name, str) or _VECTOR_TABLE_NAME.fullmatch(
                    table_name
                ) is None:
                    raise RuntimeError("registered generation vector table is invalid")
                if vector_rowid is None:
                    raise RuntimeError(
                        "registered generation vector is missing vector_rowid"
                    )
                registered_table = conn.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()
                table_kind = conn.execute(
                    """
                    SELECT type FROM pragma_table_list
                    WHERE schema = 'main' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()
                if (
                    registered_table is None
                    or table_kind is None
                    or table_kind[0] != "virtual"
                    or not _is_vec0_table_schema(registered_table[0])
                ):
                    raise RuntimeError("registered generation vector table is invalid")
                quoted_table_name = table_name.replace('"', '""')
                conn.execute(
                    f'DELETE FROM "{quoted_table_name}" WHERE rowid = ?',
                    (int(vector_rowid),),
                )
                remaining = conn.execute(
                    f'SELECT 1 FROM "{quoted_table_name}" WHERE rowid = ?',
                    (int(vector_rowid),),
                ).fetchone()
                if remaining is not None:
                    raise RuntimeError("registered generation vector delete failed")
            self._after_write_stage("purge_generation_vectors", conn)

            vector_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vec_embeddings'"
            ).fetchone()
            if vector_table is not None:
                conn.execute(
                    """
                    DELETE FROM vec_embeddings
                    WHERE id = ?
                      AND EXISTS (
                          SELECT 1 FROM memory_metadata
                          WHERE memory_id = ? AND owner_id = ? AND scope = ?
                            AND revision = ?
                      )
                    """,
                    (memory_id, memory_id, owner_id, scope, current_revision),
                )
            conn.execute(
                """
                DELETE FROM memory_embeddings
                WHERE memory_id = ? AND owner_id = ? AND scope = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ?
                  )
                """,
                (
                    memory_id,
                    owner_id,
                    scope,
                    memory_id,
                    owner_id,
                    scope,
                    current_revision,
                ),
            )
            self._after_write_stage("purge_vectors", conn)

            conn.execute(
                """
                DELETE FROM keywords
                WHERE memory_id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ?
                  )
                """,
                (memory_id, memory_id, owner_id, scope, current_revision),
            )
            self._after_write_stage("purge_keywords", conn)

            conn.execute(
                """
                DELETE FROM memories_fts
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM memory_metadata
                      WHERE memory_id = ? AND owner_id = ? AND scope = ?
                        AND revision = ?
                  )
                """,
                (memory_id, memory_id, owner_id, scope, current_revision),
            )
            self._after_write_stage("purge_fts", conn)

            metadata = conn.execute(
                """
                DELETE FROM memory_metadata
                WHERE memory_id = ? AND owner_id = ? AND scope = ? AND revision = ?
                """,
                (memory_id, owner_id, scope, current_revision),
            )
            if metadata.rowcount != 1:
                raise RevisionConflictError(current_revision)
            self._after_write_stage("purge_metadata", conn)

            memory = conn.execute(
                """
                DELETE FROM memories
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM mcp_idempotency
                      WHERE owner_id = ? AND scope = ? AND memory_id = memories.id
                        AND grant_id = ? AND operation = 'purge'
                        AND idempotency_key = ? AND purged_at = ?
                  )
                """,
                (
                    memory_id,
                    owner_id,
                    scope,
                    grant_id,
                    idempotency_key,
                    occurred_at,
                ),
            )
            if memory.rowcount != 1:
                raise RuntimeError("purge canonical memory delete failed")
            self._after_write_stage("purge_memory", conn)

            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _fts_query(query: str) -> str:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be blank")
        terms = query.split()
        return " ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

    def search_scoped(
        self,
        *,
        grant_id: str,
        scope: str,
        query: str,
        limit: int = 20,
    ) -> tuple[ScopedSearchResult, ...]:
        if not isinstance(grant_id, str) or not grant_id.strip():
            raise ValueError("grant_id must not be blank")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope must not be blank")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 through 50")

        fts_query = self._fts_query(query)
        with self._readonly_connection() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.content, mm.revision, mm.scope, mm.kind,
                       mm.content_hash, mm.source_client, mm.source_conversation,
                       mm.actor_id,
                       mm.creator_grant_id, mm.created_at, mm.updated_at
                FROM memories_fts
                JOIN memories AS m ON m.id = memories_fts.id
                JOIN memory_metadata AS mm ON mm.memory_id = m.id
                JOIN client_grants AS authorized_grant
                  ON authorized_grant.grant_id = ?
                 AND authorized_grant.owner_id = mm.owner_id
                 AND authorized_grant.revoked_at IS NULL
                 AND authorized_grant.unlinked_at IS NULL
                WHERE EXISTS (
                          SELECT 1
                          FROM json_each(
                              authorized_grant.memory_scope_patterns_json
                          ) AS authorized_scope
                          WHERE authorized_scope.type = 'text'
                            AND authorized_scope.value = mm.scope
                      )
                  AND (
                          authorized_grant.grant_kind = 'MIGRATION'
                          OR EXISTS (
                              SELECT 1
                              FROM json_each(
                                  authorized_grant.oauth_scopes_json
                              ) AS oauth_scope
                              WHERE oauth_scope.type = 'text'
                                AND oauth_scope.value IN (
                                    'memory:read', 'memory:admin'
                                )
                          )
                      )
                  AND mm.scope = ?
                  AND mm.deleted_at IS NULL
                  AND memories_fts MATCH ?
                ORDER BY bm25(memories_fts), mm.updated_at DESC, m.id
                LIMIT ?
                """,
                (grant_id, scope, fts_query, limit),
            ).fetchall()

        return tuple(ScopedSearchResult(*row) for row in rows)

    def get_scoped_memory(
        self,
        *,
        grant_id: str,
        scope: str,
        memory_id: str,
    ) -> ScopedMemoryResult | None:
        if not isinstance(grant_id, str) or not grant_id.strip():
            raise ValueError("grant_id must not be blank")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope must not be blank")
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("memory_id must not be blank")

        with self._readonly_connection() as conn:
            row = conn.execute(
                """
                SELECT m.id, m.content, mm.revision, mm.scope, mm.kind,
                       mm.tags_json, mm.content_hash, mm.source_client,
                       mm.source_conversation, mm.actor_id,
                       mm.creator_grant_id, mm.created_at, mm.updated_at
                FROM memories AS m
                JOIN memory_metadata AS mm ON mm.memory_id = m.id
                JOIN client_grants AS authorized_grant
                  ON authorized_grant.grant_id = ?
                 AND authorized_grant.owner_id = mm.owner_id
                 AND authorized_grant.revoked_at IS NULL
                 AND authorized_grant.unlinked_at IS NULL
                WHERE EXISTS (
                          SELECT 1
                          FROM json_each(
                              authorized_grant.memory_scope_patterns_json
                          ) AS authorized_scope
                          WHERE authorized_scope.type = 'text'
                            AND authorized_scope.value = mm.scope
                      )
                  AND (
                          authorized_grant.grant_kind = 'MIGRATION'
                          OR EXISTS (
                              SELECT 1
                              FROM json_each(
                                  authorized_grant.oauth_scopes_json
                              ) AS oauth_scope
                              WHERE oauth_scope.type = 'text'
                                AND oauth_scope.value IN (
                                    'memory:read', 'memory:admin'
                                )
                          )
                      )
                  AND m.id = ?
                  AND mm.scope = ?
                  AND mm.deleted_at IS NULL
                """,
                (grant_id, memory_id, scope),
            ).fetchone()

        if row is None:
            return None
        tags = tuple(json.loads(row[5])) if row[5] else ()
        return ScopedMemoryResult(
            memory_id=row[0],
            content=row[1],
            revision=row[2],
            scope=row[3],
            kind=row[4],
            tags=tags,
            content_hash=row[6],
            source_client=row[7],
            source_conversation=row[8],
            actor_id=row[9],
            creator_grant_id=row[10],
            created_at=row[11],
            updated_at=row[12],
        )

    def recent_scoped_memories(
        self,
        *,
        grant_id: str,
        scope: str,
        limit: int = 20,
    ) -> tuple[ScopedMemoryResult, ...]:
        if not isinstance(grant_id, str) or not grant_id.strip():
            raise ValueError("grant_id must not be blank")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope must not be blank")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 through 50")

        with self._readonly_connection() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.content, mm.revision, mm.scope, mm.kind,
                       mm.tags_json, mm.content_hash, mm.source_client,
                       mm.source_conversation, mm.actor_id,
                       mm.creator_grant_id, mm.created_at, mm.updated_at
                FROM memories AS m
                JOIN memory_metadata AS mm ON mm.memory_id = m.id
                JOIN client_grants AS authorized_grant
                  ON authorized_grant.grant_id = ?
                 AND authorized_grant.owner_id = mm.owner_id
                 AND authorized_grant.revoked_at IS NULL
                 AND authorized_grant.unlinked_at IS NULL
                WHERE EXISTS (
                          SELECT 1
                          FROM json_each(
                              authorized_grant.memory_scope_patterns_json
                          ) AS authorized_scope
                          WHERE authorized_scope.type = 'text'
                            AND authorized_scope.value = mm.scope
                      )
                  AND (
                          authorized_grant.grant_kind = 'MIGRATION'
                          OR EXISTS (
                              SELECT 1
                              FROM json_each(
                                  authorized_grant.oauth_scopes_json
                              ) AS oauth_scope
                              WHERE oauth_scope.type = 'text'
                                AND oauth_scope.value IN (
                                    'memory:read', 'memory:admin'
                                )
                          )
                      )
                  AND mm.scope = ?
                  AND mm.deleted_at IS NULL
                ORDER BY mm.updated_at DESC, m.id
                LIMIT ?
                """,
                (grant_id, scope, limit),
            ).fetchall()

        results = []
        for row in rows:
            tags = tuple(json.loads(row[5])) if row[5] else ()
            results.append(
                ScopedMemoryResult(
                    memory_id=row[0],
                    content=row[1],
                    revision=row[2],
                    scope=row[3],
                    kind=row[4],
                    tags=tags,
                    content_hash=row[6],
                    source_client=row[7],
                    source_conversation=row[8],
                    actor_id=row[9],
                    creator_grant_id=row[10],
                    created_at=row[11],
                    updated_at=row[12],
                )
            )
        return tuple(results)
