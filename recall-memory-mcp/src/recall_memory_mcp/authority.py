"""Concrete authority wiring (prerequisite for task 6.3 ``serve``).

``service.py`` and ``app.py`` are deliberately storage-free: they only know
the protocols in ``repository.py``.  This module is the *only* place where
the distribution is allowed to bind those protocols to the canonical
``recall-sqlite`` core, and it supplies the three things ``create_app`` needs
before a server can actually serve:

1. :class:`SqliteAuthorityRepository` — ``recall.mcp_repository`` already
   implements every scoped read/write with owner+scope SQL predicates, but it
   is missing ``health()`` / ``scope_cardinality()`` and it raises its own
   exception classes.  This adapter adds the two methods and translates the
   core taxonomy into ``recall_memory_mcp.repository`` errors so the service
   maps them to the right wire codes instead of ``internal_error``.
2. :class:`RecallEmbedder` — text to one blob per non-retired embedding
   generation.  The vector provider is ``recall.embed`` (an OpenAI-compatible
   ``/v1/embeddings`` endpoint); when it is unreachable the embedder raises
   :class:`~recall_memory_mcp.repository.EmbeddingUnavailableError` instead of
   inventing a vector.
3. A request-scoped :data:`~recall_memory_mcp.server.ContextProvider`.  A
   plain ASGI middleware cannot be used for this: on stateful Streamable HTTP
   the tool handler runs in the session manager's task, not in the HTTP
   request task, so an ASGI-set ``ContextVar`` is invisible downstream.  The
   identity is therefore established by an **MCP server middleware**, which
   the SDK runs in the handler's own task and hands the per-message HTTP
   request (``ServerRequestContext.request``).

Hard rules kept here:

* The caller identity is always resolved from the verified connection and the
  ``client_grants`` table — never from a tool payload, and never from the
  token's own scope claim alone (the DB grant is the upper bound, R5.6).
* Embedding profiles are provisioned per ``(owner, scope)`` with the identity
  of the *running* embedder.  If a scope already carries a profile written by
  a different provider/model/dimension we fail closed rather than mixing
  incomparable vectors into one generation.
* Nothing in here ever touches the operator's default Recall database: every
  path comes from validated :class:`ServerSettings`.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import struct
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from . import __version__, provisioning, redaction
from . import repository as repo
from .service import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE, CallerContext, RecallMemoryService
from .settings import ServerMode, ServerSettings

logger = logging.getLogger("recall_memory_mcp.authority")

#: Generation used by an embedder that has no opinion about versioning.  The
#: repository adapter re-keys it onto the generations that actually exist for
#: the target scope, so the embedder never has to know about them.
DEFAULT_GENERATION: Final[int] = 1

DIGEST_KEY_FILE: Final[str] = "digest.key"
DIGEST_KEY_ENV: Final[str] = "RECALL_MCP_DIGEST_KEY"
TOKEN_SECRET_ENV: Final[str] = "RECALL_MCP_TOKEN_SECRET"

LOCAL_ISSUER: Final[str] = "local-loopback"
LOCAL_CLIENT_ID: Final[str] = "local-operator"
DEFAULT_MEMORY_SCOPES: Final[tuple[str, ...]] = ("global",)
LOCAL_OAUTH_SCOPES: Final[tuple[str, ...]] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)


class AuthorityError(RuntimeError):
    """The authority database cannot back a running server."""


class AuthorityNotInitialisedError(AuthorityError):
    """``init`` has not been run (or the database failed verification)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# embedder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingIdentity:
    """Provenance of the vector provider, stored on every embedding row.

    ``endpoint_identity_hash`` is a digest, never the URL: an endpoint can
    embed a hostname, a port, or (in a misconfiguration) a credential, and
    provenance must stay safe to read back out of the database.
    """

    provider: str
    model: str
    endpoint_identity_hash: str


def endpoint_identity_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


class RecallEmbedder:
    """``recall.embed`` adapter implementing the ``Embedder`` protocol.

    ``recall/__init__.py`` re-exports the *function* ``embed``, so
    ``from recall import embed`` silently yields a function rather than the
    module.  The module is therefore imported by name.
    """

    def __init__(
        self,
        *,
        embed_fn: Callable[[str], Sequence[float] | None] | None = None,
        provider: str = "openai-compatible",
        model: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        module: Any = None
        if embed_fn is None or model is None or endpoint is None:
            module = self._load_module()
        self._embed = embed_fn if embed_fn is not None else module.embed
        resolved_model = model if model is not None else getattr(module, "EMBED_MODEL", "unknown")
        resolved_endpoint = (
            endpoint if endpoint is not None else getattr(module, "EMBED_URL", "unknown")
        )
        self._identity = EmbeddingIdentity(
            provider=provider,
            model=resolved_model,
            endpoint_identity_hash=endpoint_identity_hash(resolved_endpoint),
        )

    @staticmethod
    def _load_module() -> Any:
        import importlib  # noqa: PLC0415 - deliberately lazy

        try:
            return importlib.import_module("recall.embed")
        except Exception as exc:  # noqa: BLE001
            raise repo.EmbeddingUnavailableError(
                "the canonical recall-sqlite core is not installed"
            ) from exc

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._identity

    def encode(self, text: str) -> dict[int, bytes]:
        try:
            vector = self._embed(text)
        except Exception as exc:  # noqa: BLE001 - a provider failure is never fatal
            logger.warning("embedding provider failed: %s", redaction.redact_exception(exc))
            raise repo.EmbeddingUnavailableError("embedding provider is unreachable") from exc
        if not vector:
            raise repo.EmbeddingUnavailableError("embedding provider is unreachable")
        return {DEFAULT_GENERATION: pack_vector(vector)}


def pack_vector(vector: Iterable[float]) -> bytes:
    """Little-endian float32, the layout ``sqlite-vec`` reads."""

    values = [float(component) for component in vector]
    if not values:
        raise repo.EmbeddingUnavailableError("embedding provider returned an empty vector")
    return struct.pack(f"<{len(values)}f", *values)


# ---------------------------------------------------------------------------
# repository adapter
# ---------------------------------------------------------------------------

#: Grant predicate shared by every read.  Copied deliberately from
#: ``recall.mcp_repository`` so a cardinality probe can never be broader than
#: the reads it describes.
_READ_GRANT_PREDICATE = """
    JOIN client_grants AS authorized_grant
      ON authorized_grant.grant_id = ?
     AND authorized_grant.owner_id = mm.owner_id
     AND authorized_grant.revoked_at IS NULL
     AND authorized_grant.unlinked_at IS NULL
    WHERE EXISTS (
              SELECT 1
              FROM json_each(authorized_grant.memory_scope_patterns_json)
                   AS authorized_scope
              WHERE authorized_scope.type = 'text'
                AND authorized_scope.value = mm.scope
          )
      AND (
              authorized_grant.grant_kind = 'MIGRATION'
              OR EXISTS (
                  SELECT 1
                  FROM json_each(authorized_grant.oauth_scopes_json) AS oauth_scope
                  WHERE oauth_scope.type = 'text'
                    AND oauth_scope.value IN ('memory:read', 'memory:admin')
              )
          )
      AND mm.scope = ?
      AND mm.deleted_at IS NULL
"""


def _load_core_repository() -> Any:
    try:
        from recall import mcp_repository  # noqa: PLC0415 - deliberately lazy
    except Exception as exc:  # noqa: BLE001
        raise AuthorityError("the canonical recall-sqlite core is not installed") from exc
    return mcp_repository


class SqliteAuthorityRepository:
    """``MemoryRepository`` over the canonical authority database.

    Composition rather than inheritance: the core class is imported lazily so
    importing this module never requires ``recall-sqlite`` to be installed
    (``doctor`` has to be able to *report* a missing core).
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        identity: EmbeddingIdentity,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.db_path = Path(db_path)
        self.identity = identity
        self.clock = clock
        self._core = _load_core_repository()
        self._delegate = self._core.RecallMCPRepository(self.db_path)

    # -- error translation ----------------------------------------------
    def _translate(self, exc: BaseException) -> BaseException:
        core = self._core
        if isinstance(exc, repo.RepositoryError):
            return exc
        if isinstance(exc, PermissionError):
            return repo.NotAuthorizedError()
        if isinstance(exc, core.RevisionConflictError):
            return repo.RevisionConflictError(current_revision=int(exc.current_revision))
        if isinstance(exc, core.IdempotencyKeyReusedError):
            return repo.IdempotencyKeyReusedError()
        if isinstance(exc, core.PurgedMemoryReplayError):
            return repo.IdempotencyKeyPurgedError()
        if isinstance(exc, ValueError):
            return repo.RepositoryValidationError(str(exc))
        if isinstance(exc, sqlite3.Error):
            logger.error("authority database failure: %s", redaction.redact_exception(exc))
            return repo.RepositoryError("authority database failure")
        return exc

    def _call(self, name: str, /, **kwargs: Any) -> Any:
        try:
            return getattr(self._delegate, name)(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised as a taxonomy error
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    # -- connections ------------------------------------------------------
    def _readonly(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise repo.RepositoryError("authority database is not initialised")
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _writable(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- reads -------------------------------------------------------------
    def search_scoped(self, **kwargs: Any) -> tuple[Any, ...]:
        return self._call("search_scoped", **kwargs)

    def get_scoped_memory(self, **kwargs: Any) -> Any | None:
        return self._call("get_scoped_memory", **kwargs)

    def recent_scoped_memories(self, **kwargs: Any) -> tuple[Any, ...]:
        return self._call("recent_scoped_memories", **kwargs)

    def health(self) -> repo.RepositoryHealth:
        """Reachability + schema version. Never discloses a path or a count."""

        try:
            conn = self._readonly()
        except (repo.RepositoryError, sqlite3.Error) as exc:
            logger.warning("authority unreachable: %s", redaction.redact_exception(exc))
            return repo.RepositoryHealth(
                reachable=False, schema_version=0, embedding_generation=None
            )
        try:
            version = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            )
            row = conn.execute(
                "SELECT MAX(generation) FROM embedding_profiles WHERE status = 'ACTIVE'"
            ).fetchone()
            generation = None if row is None or row[0] is None else int(row[0])
        except sqlite3.Error as exc:
            logger.warning("authority health probe failed: %s", redaction.redact_exception(exc))
            return repo.RepositoryHealth(
                reachable=False, schema_version=0, embedding_generation=None
            )
        finally:
            conn.close()
        return repo.RepositoryHealth(
            reachable=True, schema_version=version, embedding_generation=generation
        )

    def scope_cardinality(self, *, grant_id: str, scope: str) -> int:
        """Live rows in one authorized scope, using the read grant predicate.

        The exact number never leaves the service: ``memory_status`` buckets
        it and suppresses small scopes (task 3.8).
        """

        if not isinstance(grant_id, str) or not grant_id.strip():
            raise repo.RepositoryValidationError("grant_id must not be blank")
        if not isinstance(scope, str) or not scope.strip():
            raise repo.RepositoryValidationError("scope must not be blank")
        conn = self._readonly()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_metadata AS mm" + _READ_GRANT_PREDICATE,
                (grant_id, scope),
            ).fetchone()
        except sqlite3.Error as exc:
            raise repo.RepositoryError("authority database failure") from exc
        finally:
            conn.close()
        return int(row[0]) if row else 0

    # -- owner-scoped admin operations (tasks 6.10 / 6.10a / 6.10b) --------
    #: Export is an *admin* read.  It is intentionally NOT the shared read
    #: predicate: ``memory:read`` must never be able to bulk-dump a scope,
    #: and a ``MIGRATION`` grant must never be able to export at all.
    _ADMIN_EXPORT_SQL = """
        SELECT m.id, m.content, mm.revision, mm.scope, mm.kind, mm.tags_json,
               mm.content_hash, mm.source_client, mm.source_conversation,
               mm.created_at, mm.updated_at, mm.deleted_at
        FROM memory_metadata AS mm
        JOIN memories AS m ON m.id = mm.memory_id
        JOIN client_grants AS authorized_grant
          ON authorized_grant.grant_id = ?
         AND authorized_grant.owner_id = mm.owner_id
         AND authorized_grant.grant_kind = 'OAUTH'
         AND authorized_grant.revoked_at IS NULL
         AND authorized_grant.unlinked_at IS NULL
        WHERE EXISTS (
                  SELECT 1
                  FROM json_each(authorized_grant.memory_scope_patterns_json)
                       AS authorized_scope
                  WHERE authorized_scope.type = 'text'
                    AND authorized_scope.value = mm.scope
              )
          AND EXISTS (
                  SELECT 1
                  FROM json_each(authorized_grant.oauth_scopes_json) AS oauth_scope
                  WHERE oauth_scope.type = 'text'
                    AND oauth_scope.value = 'memory:admin'
              )
          AND mm.scope = ?
          AND (? = 1 OR mm.deleted_at IS NULL)
        ORDER BY mm.created_at, m.id
    """

    def export_scoped(
        self, *, grant_id: str, scope: str, include_tombstones: bool = False
    ) -> tuple[dict[str, Any], ...]:
        """Owner-scoped bulk read for ``memory export`` (R8.4 / R8.8).

        Authorization is expressed in SQL exactly like every other read, so
        knowing a ``memory_id`` — or holding a read-only grant — cannot widen
        the result set.  Tombstones are excluded unless the caller explicitly
        asked for them, and the flag travels back out so the export document
        can state what it contains.
        """

        if not isinstance(grant_id, str) or not grant_id.strip():
            raise repo.RepositoryValidationError("grant_id must not be blank")
        if not isinstance(scope, str) or not scope.strip():
            raise repo.RepositoryValidationError("scope must not be blank")
        if not isinstance(include_tombstones, bool):
            raise repo.RepositoryValidationError("include_tombstones must be a boolean")

        conn = self._readonly()
        try:
            rows = conn.execute(
                self._ADMIN_EXPORT_SQL,
                (grant_id, scope, 1 if include_tombstones else 0),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.error("authority export failed: %s", redaction.redact_exception(exc))
            raise repo.RepositoryError("authority database failure") from exc
        finally:
            conn.close()

        exported: list[dict[str, Any]] = []
        for row in rows:
            exported.append(
                {
                    "memory_id": str(row[0]),
                    "content": str(row[1]),
                    "revision": int(row[2]),
                    "scope": str(row[3]),
                    "kind": str(row[4]),
                    "tags": tuple(json.loads(row[5])) if row[5] else (),
                    "content_hash": str(row[6]),
                    "source_client": str(row[7]),
                    "source_conversation": row[8],
                    "created_at": str(row[9]),
                    "updated_at": str(row[10]),
                    "deleted_at": row[11],
                }
            )
        return tuple(exported)

    def restore_memory(self, **kwargs: Any) -> Any:
        """Owner-admin row-level restore (core re-authorizes in SQL)."""

        return self._call("restore_memory", **kwargs)

    def purge_memory(self, **kwargs: Any) -> Any:
        """Owner-admin irreversible purge (core re-authorizes in SQL)."""

        return self._call("purge_memory", **kwargs)

    # -- embedding profiles -----------------------------------------------
    def _owner_for_write(self, *, grant_id: str, scopes: Sequence[str]) -> str:
        """Owner behind an *active* write grant that covers every scope.

        This is only used to decide which embedding profile row to provision;
        the authoritative check still runs inside the core transaction.
        """

        clauses = " ".join(
            "AND EXISTS (SELECT 1 FROM json_each(memory_scope_patterns_json) "
            "WHERE type = 'text' AND value = ?)"
            for _ in scopes
        )
        conn = self._readonly()
        try:
            row = conn.execute(
                f"""
                SELECT owner_id FROM client_grants
                WHERE grant_id = ?
                  AND grant_kind = 'OAUTH'
                  AND revoked_at IS NULL
                  AND unlinked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_each(oauth_scopes_json)
                      WHERE type = 'text' AND value IN ('memory:write', 'memory:admin')
                  )
                  {clauses}
                """,
                (grant_id, *scopes),
            ).fetchone()
        except sqlite3.Error as exc:
            raise repo.RepositoryError("authority database failure") from exc
        finally:
            conn.close()
        if row is None:
            raise repo.NotAuthorizedError()
        return str(row[0])

    def ensure_embedding_profile(
        self, *, owner_id: str, scope: str, dimension: int
    ) -> tuple[int, ...]:
        """Return every non-retired generation for ``(owner, scope)``.

        Creates generation 1 the first time a scope is written to.  A scope
        whose existing profile was produced by a different provider, model or
        dimension fails closed: silently writing an incomparable vector into
        that generation would corrupt every later search.
        """

        if dimension < 1:
            raise repo.EmbeddingUnavailableError("embedding vector is empty")
        conn = self._writable()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT generation, provider, endpoint_identity_hash, model, dimension
                FROM embedding_profiles
                WHERE owner_id = ? AND scope = ? AND status IN ('ACTIVE', 'BUILDING')
                ORDER BY generation
                """,
                (owner_id, scope),
            ).fetchall()
            if rows:
                for generation, provider, endpoint_hash, model, stored_dimension in rows:
                    if (
                        provider != self.identity.provider
                        or endpoint_hash != self.identity.endpoint_identity_hash
                        or model != self.identity.model
                        or int(stored_dimension) != dimension
                    ):
                        raise repo.EmbeddingUnavailableError(
                            "the running embedder does not match the stored embedding profile"
                        )
                conn.rollback()
                return tuple(int(row[0]) for row in rows)

            now = self.clock()
            conn.execute(
                """
                INSERT INTO embedding_profiles (
                    owner_id, scope, generation, provider, endpoint_identity_hash,
                    model, dimension, status, created_at, activated_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                ON CONFLICT DO NOTHING
                """,
                (
                    owner_id,
                    scope,
                    DEFAULT_GENERATION,
                    self.identity.provider,
                    self.identity.endpoint_identity_hash,
                    self.identity.model,
                    dimension,
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise repo.RepositoryError("authority database failure") from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return (DEFAULT_GENERATION,)

    def _prepare_blobs(
        self, *, grant_id: str, scopes: Sequence[str], blobs: Any
    ) -> dict[int, bytes]:
        """Fan one provider vector out onto the scope's real generations."""

        if not isinstance(blobs, dict) or not blobs:
            raise repo.RepositoryValidationError("embedding_blobs must not be empty")
        if set(blobs) != {DEFAULT_GENERATION}:
            # An embedder that already speaks generations owns the mapping.
            return dict(blobs)
        blob = blobs[DEFAULT_GENERATION]
        if not isinstance(blob, bytes) or not blob or len(blob) % 4:
            raise repo.EmbeddingUnavailableError("embedding vector is malformed")
        dimension = len(blob) // 4

        owner_id = self._owner_for_write(grant_id=grant_id, scopes=scopes)
        generations: set[int] = set()
        for scope in scopes:
            generations.update(
                self.ensure_embedding_profile(
                    owner_id=owner_id, scope=scope, dimension=dimension
                )
            )
        return {generation: blob for generation in sorted(generations)}

    # -- writes ------------------------------------------------------------
    def add_memory(self, **kwargs: Any) -> repo.AddOutcome:
        kwargs = dict(kwargs)
        kwargs["embedding_blobs"] = self._prepare_blobs(
            grant_id=kwargs.get("grant_id", ""),
            scopes=(kwargs.get("scope", ""),),
            blobs=kwargs.get("embedding_blobs"),
        )
        result = self._call("add_memory", **kwargs)
        return repo.AddOutcome(
            memory_id=result.memory_id, revision=result.revision, created_at=result.created_at
        )

    def replace_memory(self, **kwargs: Any) -> repo.ReplaceOutcome:
        kwargs = dict(kwargs)
        scope = kwargs.get("scope", "")
        new_scope = kwargs.get("new_scope")
        scopes = (scope,) if new_scope in (None, scope) else (scope, new_scope)
        kwargs["embedding_blobs"] = self._prepare_blobs(
            grant_id=kwargs.get("grant_id", ""),
            scopes=scopes,
            blobs=kwargs.get("embedding_blobs"),
        )
        result = self._call("replace_memory", **kwargs)
        return repo.ReplaceOutcome(
            memory_id=result.memory_id, revision=result.revision, updated_at=result.updated_at
        )

    def remove_memory(self, **kwargs: Any) -> repo.RemoveOutcome:
        result = self._call("remove_memory", **kwargs)
        return repo.RemoveOutcome(
            memory_id=result.memory_id, revision=result.revision, deleted_at=result.deleted_at
        )


# ---------------------------------------------------------------------------
# local identity / grants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityGrant:
    grant_id: str
    owner_id: str
    source_client: str
    oauth_scopes: tuple[str, ...]
    memory_scopes: tuple[str, ...]

    def caller_context(self, *, actor_id: str | None = None) -> CallerContext:
        return CallerContext(
            grant_id=self.grant_id,
            owner_id=self.owner_id,
            actor_id=actor_id or f"actor:{self.source_client}",
            source_client=self.source_client,
            oauth_scopes=self.oauth_scopes,
            memory_scopes=self.memory_scopes,
        )


def _single_owner(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT owner_id FROM owners ORDER BY owner_id").fetchall()
    if not rows:
        raise AuthorityNotInitialisedError("the authority database has no owner")
    if len(rows) > 1:
        raise AuthorityError(
            "the authority database has more than one owner; local mode is single-owner"
        )
    return str(rows[0][0])


def ensure_local_grant(
    db_path: str | Path,
    *,
    memory_scopes: Sequence[str] = DEFAULT_MEMORY_SCOPES,
    oauth_scopes: Sequence[str] = LOCAL_OAUTH_SCOPES,
    client_id: str = LOCAL_CLIENT_ID,
    now: str | None = None,
) -> AuthorityGrant:
    """Idempotently bind the loopback operator to the single local owner.

    This is *not* an OAuth shortcut for remote clients: the grant only exists
    so a loopback-only server has a real, auditable ``client_grants`` row to
    authorize against instead of trusting an in-memory object.  Every change
    is recorded in ``owner_link_events`` (R5.6c).
    """

    path = Path(db_path)
    if not path.is_file():
        raise AuthorityNotInitialisedError("no authority database; run `init` first")
    scopes = tuple(dict.fromkeys(memory_scopes))
    if not scopes:
        raise AuthorityError("at least one memory scope is required")
    grants = tuple(dict.fromkeys(oauth_scopes))
    timestamp = now or _utc_now()

    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner_id = _single_owner(conn)
        row = conn.execute(
            """
            SELECT grant_id, source_client, oauth_scopes_json, memory_scope_patterns_json,
                   grant_generation
            FROM client_grants
            WHERE issuer = ? AND subject = ? AND client_id = ?
              AND revoked_at IS NULL AND unlinked_at IS NULL
            ORDER BY grant_generation DESC
            LIMIT 1
            """,
            (LOCAL_ISSUER, owner_id, client_id),
        ).fetchone()

        if row is not None:
            stored_scopes = tuple(json.loads(row[3]))
            stored_oauth = tuple(json.loads(row[2]))
            if set(stored_scopes) >= set(scopes) and set(stored_oauth) >= set(grants):
                conn.rollback()
                return AuthorityGrant(
                    grant_id=str(row[0]),
                    owner_id=owner_id,
                    source_client=str(row[1]),
                    oauth_scopes=stored_oauth,
                    memory_scopes=stored_scopes,
                )
            generation = int(row[4]) + 1
            merged_scopes = tuple(dict.fromkeys((*stored_scopes, *scopes)))
            merged_oauth = tuple(dict.fromkeys((*stored_oauth, *grants)))
            operation = "scope_change"
            conn.execute(
                "UPDATE client_grants SET unlinked_at = ? WHERE grant_id = ?",
                (timestamp, row[0]),
            )
        else:
            generation = 1
            merged_scopes = scopes
            merged_oauth = grants
            operation = "initial_bind"

        # ``grant_id`` must be unique per (owner, client, generation). Leaving
        # the client out made a second local client (for example a seeding or
        # migration identity) collide with the operator grant on
        # ``UNIQUE(grant_id, owner_id)`` and fail to bind at all. The default
        # client keeps its historical id so existing databases are unaffected.
        client_suffix = (
            ""
            if client_id == LOCAL_CLIENT_ID
            else "-" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:8]
        )
        grant_id = f"grant-local-{owner_id}{client_suffix}-{generation}"
        conn.execute(
            """
            INSERT INTO client_grants (
                grant_id, owner_id, issuer, subject, client_id, grant_generation,
                grant_kind, source_client, oauth_scopes_json,
                memory_scope_patterns_json, binding_challenge_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'OAUTH', ?, ?, ?, NULL, ?)
            """,
            (
                grant_id,
                owner_id,
                LOCAL_ISSUER,
                owner_id,
                client_id,
                generation,
                client_id,
                _canonical_json(list(merged_oauth)),
                _canonical_json(list(merged_scopes)),
                timestamp,
            ),
        )
        details = _canonical_json(
            {
                "client_id": client_id,
                "generation": generation,
                "issuer": LOCAL_ISSUER,
                "memory_scopes": list(merged_scopes),
                "oauth_scopes": list(merged_oauth),
                "operation": operation,
            }
        )
        conn.execute(
            """
            INSERT INTO owner_link_events (
                link_event_id, owner_id, grant_id, challenge_id, operation,
                actor_id, occurred_at, details_digest
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                f"link-{hashlib.sha256(f'{grant_id}:{operation}'.encode()).hexdigest()[:24]}",
                owner_id,
                grant_id,
                operation,
                f"actor:{client_id}",
                timestamp,
                hashlib.sha256(details.encode("utf-8")).hexdigest(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise AuthorityError("could not bind the local operator grant") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return AuthorityGrant(
        grant_id=grant_id,
        owner_id=owner_id,
        source_client=client_id,
        oauth_scopes=merged_oauth,
        memory_scopes=merged_scopes,
    )


def lookup_grant(
    db_path: str | Path, *, issuer: str, subject: str, client_id: str
) -> AuthorityGrant | None:
    """The most recent *active* grant for one verified OAuth identity."""

    path = Path(db_path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            """
            SELECT grant_id, owner_id, source_client, oauth_scopes_json,
                   memory_scope_patterns_json
            FROM client_grants
            WHERE issuer = ? AND subject = ? AND client_id = ?
              AND grant_kind = 'OAUTH'
              AND revoked_at IS NULL AND unlinked_at IS NULL
            ORDER BY grant_generation DESC
            LIMIT 1
            """,
            (issuer, subject, client_id),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.error("grant lookup failed: %s", redaction.redact_exception(exc))
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return AuthorityGrant(
        grant_id=str(row[0]),
        owner_id=str(row[1]),
        source_client=str(row[2]),
        oauth_scopes=tuple(json.loads(row[3])),
        memory_scopes=tuple(json.loads(row[4])),
    )


# ---------------------------------------------------------------------------
# request-scoped caller context
# ---------------------------------------------------------------------------

_CURRENT_CALLER: contextvars.ContextVar[CallerContext | None] = contextvars.ContextVar(
    "recall_memory_mcp_caller", default=None
)

#: ``token -> CallerContext | None``.  ``None`` means "not authorized"; the
#: resolver must never raise a message that distinguishes *why*.
CallerResolver = Callable[[str | None], "CallerContext | None"]


def current_caller() -> CallerContext:
    caller = _CURRENT_CALLER.get()
    if caller is None:
        raise repo.NotAuthorizedError()
    return caller


def bearer_token(headers: Mapping[str, str] | None) -> str | None:
    if not headers:
        return None
    raw = headers.get("authorization") or headers.get("Authorization")
    if not raw:
        return None
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def caller_context_middleware(resolver: CallerResolver) -> Any:
    """MCP server middleware establishing the per-request caller identity.

    Registered on ``MCPServer`` (not on Starlette) because the tool handler
    runs in the session manager's task: an ASGI-set ``ContextVar`` would not
    be visible here, but a server middleware runs in the handler's own task.
    """

    async def middleware(ctx: Any, call_next: Any) -> Any:
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        try:
            caller = resolver(bearer_token(headers))
        except Exception as exc:  # noqa: BLE001 - never leak the reason
            logger.warning("caller resolution failed: %s", redaction.redact_exception(exc))
            caller = None
        token = _CURRENT_CALLER.set(caller)
        try:
            return await call_next(ctx)
        finally:
            _CURRENT_CALLER.reset(token)

    return middleware


def static_resolver(grant: AuthorityGrant) -> CallerResolver:
    """Loopback resolver: one bound local grant, regardless of headers."""

    context = grant.caller_context()

    def resolve(_token: str | None) -> CallerContext | None:
        return context

    return resolve


def token_resolver(
    db_path: str | Path, *, token_manager: Any, issuer: str | None = None
) -> CallerResolver:
    """Bearer-token resolver: verified token -> stored grant.

    The token proves *which* OAuth identity is calling; the database decides
    what that identity may do.  Scopes are intersected with the stored grant
    so a forged or over-broad ``scope`` claim can never widen access (R5.6).
    """

    def resolve(token: str | None) -> CallerContext | None:
        if not token:
            return None
        try:
            payload = token_manager.validate_token(token)
        except Exception as exc:  # noqa: BLE001 - uniform denial
            logger.info("token rejected: %s", redaction.redact_exception(exc))
            return None
        grant = lookup_grant(
            db_path,
            issuer=issuer or payload.iss,
            subject=payload.sub,
            client_id=payload.client_id,
        )
        if grant is None:
            return None
        granted = tuple(scope for scope in grant.oauth_scopes if scope in payload.scopes)
        if not granted:
            return None
        return CallerContext(
            grant_id=grant.grant_id,
            owner_id=grant.owner_id,
            actor_id=f"actor:{payload.sub}",
            source_client=grant.source_client,
            oauth_scopes=granted,
            memory_scopes=grant.memory_scopes,
        )

    return resolve


class RequireBearerMiddleware:
    """ASGI guard: no verified bearer token, no MCP endpoint (401).

    The MCP layer already fails closed without an identity, but a protected
    resource has to answer an anonymous request with ``401`` +
    ``WWW-Authenticate`` so a client knows to start the OAuth flow.
    """

    def __init__(self, app: Any, *, resolver: CallerResolver, path: str = "/mcp") -> None:
        self.app = app
        self.resolver = resolver
        self.path = path

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith(self.path):
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            try:
                caller = self.resolver(bearer_token(headers))
            except Exception:  # noqa: BLE001
                caller = None
            if caller is None:
                from starlette.responses import JSONResponse  # noqa: PLC0415

                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="recall-memory-mcp"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# digest key
# ---------------------------------------------------------------------------


def load_or_create_digest_key(config_dir: str | Path, env: Mapping[str, str] | None = None) -> bytes:
    """Stable keyed-digest secret for idempotency payload digests.

    It must survive a restart: a new key would make a replayed idempotency
    key look like a *different* payload and turn a legitimate retry into
    ``idempotency_key_reused`` (task 1.17).  It is a secret, so it lives in
    its own owner-only file rather than in ``config.toml``.
    """

    env = dict(os.environ) if env is None else env
    override = (env.get(DIGEST_KEY_ENV) or "").strip()
    if override:
        try:
            return bytes.fromhex(override)
        except ValueError as exc:
            raise AuthorityError(f"{DIGEST_KEY_ENV} must be hex-encoded") from exc

    path = Path(config_dir) / DIGEST_KEY_FILE
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise AuthorityError("the stored digest key is corrupt") from exc
        if len(key) < 16:
            raise AuthorityError("the stored digest key is too short")
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(key.hex() + "\n")
    provisioning.harden_file(path)
    return key


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def require_ready_authority(settings: ServerSettings) -> None:
    status = provisioning.database_status(settings.db_path)
    if not status.exists:
        raise AuthorityNotInitialisedError("no authority database; run `init` first")
    if not status.ready:
        raise AuthorityNotInitialisedError("the authority database failed verification")


def build_service(
    settings: ServerSettings,
    *,
    embedder: Any | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], str] | None = None,
    digest_key: bytes | None = None,
) -> RecallMemoryService:
    """Wire the storage-free service onto the real authority database."""

    require_ready_authority(settings)
    embedder = embedder if embedder is not None else RecallEmbedder()
    identity = getattr(embedder, "identity", None)
    if not isinstance(identity, EmbeddingIdentity):
        raise AuthorityError("the embedder does not expose an EmbeddingIdentity")
    repository = SqliteAuthorityRepository(settings.db_path, identity=identity)
    return RecallMemoryService(
        repository=repository,
        embedder=embedder,
        digest_key=digest_key
        if digest_key is not None
        else load_or_create_digest_key(settings.config_dir, env),
        server_version=__version__,
        clock=clock or _utc_now,
    )


def build_app(
    settings: ServerSettings,
    *,
    service: RecallMemoryService | None = None,
    embedder: Any | None = None,
    env: Mapping[str, str] | None = None,
    memory_scopes: Sequence[str] = DEFAULT_MEMORY_SCOPES,
    resolver: CallerResolver | None = None,
    token_manager: Any | None = None,
    admin_session: bool = True,
) -> Any:
    """Build the production ASGI app bound to the authority database."""

    from .app import create_app  # noqa: PLC0415 - avoids importing starlette at CLI import

    env = dict(os.environ) if env is None else dict(env)
    digest_key = load_or_create_digest_key(settings.config_dir, env)
    service = service if service is not None else build_service(
        settings, embedder=embedder, env=env, digest_key=digest_key
    )

    require_auth = settings.require_auth
    local_grant: AuthorityGrant | None = None
    if resolver is None:
        if require_auth:
            resolver = token_resolver(
                settings.db_path,
                token_manager=token_manager or _default_token_manager(env),
            )
        else:
            if settings.mode is not ServerMode.LOCAL or settings.host not in {
                "127.0.0.1",
                "::1",
                "localhost",
            }:
                raise AuthorityError("only loopback local mode may run without authentication")
            local_grant = ensure_local_grant(settings.db_path, memory_scopes=memory_scopes)
            resolver = static_resolver(local_grant)

    admin_routes = build_authority_admin_routes(
        settings,
        service=service,
        digest_key=digest_key,
        resolver=resolver,
        memory_scopes=memory_scopes,
        local_grant=local_grant,
        enabled=admin_session,
    )

    app = create_app(
        service,
        settings,
        context_provider=current_caller,
        server_middleware=(caller_context_middleware(resolver),),
        extra_routes=admin_routes,
    )
    if require_auth:
        app.add_middleware(RequireBearerMiddleware, resolver=resolver)
    return app


def build_authority_admin_routes(
    settings: ServerSettings,
    *,
    service: RecallMemoryService,
    digest_key: bytes,
    resolver: CallerResolver,
    memory_scopes: Sequence[str] = DEFAULT_MEMORY_SCOPES,
    local_grant: "AuthorityGrant | None" = None,
    enabled: bool = True,
) -> tuple[tuple[str, tuple[str, ...], Any], ...]:
    """Admin routes + the OS-bound local admin session behind them (6.10a).

    The local session is *not* a privilege escalation: its token file is
    owner-only, so the only account that can read it is the account that
    already runs the authority and could open the database file directly.
    What the session buys is that the CLI still goes through the online
    authority — with owner derivation, scope checks and an audit trail —
    instead of growing an offline direct-DB twin.
    """

    from . import admin as admin_mod  # noqa: PLC0415
    from . import adminapi  # noqa: PLC0415

    admin_service = admin_mod.build_admin_service(
        settings, repository=service.repository, digest_key=digest_key
    )

    session = None
    context_factory: Any = None
    if enabled:
        try:
            grant = local_grant or ensure_local_grant(
                settings.db_path, memory_scopes=memory_scopes
            )
        except AuthorityError as exc:
            logger.warning(
                "local admin session disabled: %s", redaction.redact_exception(exc)
            )
        else:
            session = adminapi.create_local_admin_session(
                settings.config_dir,
                endpoint=f"http://{settings.host}:{settings.port}",
            )
            context_factory = grant.caller_context

    # SECURITY: in loopback local mode the MCP resolver is a *static* one —
    # it returns the local grant for any header at all, because reaching the
    # loopback port is the whole authentication story for model tools. Admin
    # operations are irreversible, so they must not inherit that: handing the
    # static resolver to the authenticator would let any local process purge
    # memories with an arbitrary bearer token. Only a resolver that actually
    # verifies a token (``require_auth``) is accepted as an admin credential;
    # otherwise the owner-only session file is the sole admin credential.
    authenticator = adminapi.AdminAuthenticator(
        session=session,
        local_context_factory=context_factory,
        oauth_resolver=resolver if settings.require_auth else None,
    )
    return adminapi.build_admin_routes(admin_service, authenticator)


def _default_token_manager(env: Mapping[str, str]) -> Any:
    from .auth import SimpleTokenManager  # noqa: PLC0415

    raw = (env.get(TOKEN_SECRET_ENV) or "").strip()
    if not raw:
        raise AuthorityError(
            f"{TOKEN_SECRET_ENV} is required when authentication is enabled"
        )
    return SimpleTokenManager(raw.encode("utf-8"))


__all__ = [
    "DEFAULT_GENERATION",
    "DEFAULT_MEMORY_SCOPES",
    "AuthorityError",
    "AuthorityGrant",
    "AuthorityNotInitialisedError",
    "CallerResolver",
    "EmbeddingIdentity",
    "RecallEmbedder",
    "RequireBearerMiddleware",
    "SqliteAuthorityRepository",
    "bearer_token",
    "build_app",
    "build_authority_admin_routes",
    "build_service",
    "caller_context_middleware",
    "current_caller",
    "endpoint_identity_hash",
    "ensure_local_grant",
    "load_or_create_digest_key",
    "lookup_grant",
    "pack_vector",
    "require_ready_authority",
    "static_resolver",
    "token_resolver",
]
