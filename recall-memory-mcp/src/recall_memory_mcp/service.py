"""Transport-free memory service (task 2.7).

This module deliberately imports nothing from the MCP SDK, no ASGI stack and
no concrete storage package: it depends only on the protocols in
``repository`` and the types in ``models``.  That is what makes gate 2's
"service testable with no network" achievable.

Invariants enforced here:

* R4  — ``actor_id``/``source_client``/``grant_id`` come from the
  authenticated caller context, never from the tool payload.
* R7.3/R7.8 — OAuth scope *and* exact memory scope are checked before the
  repository is touched at all.
* R5  — repository conflicts map to stable, content-free error codes.
* R8.5 — unexpected failures never leak a path, a key or a traceback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import models, redaction
from . import repository as repo

logger = logging.getLogger("recall_memory_mcp.service")

SCOPE_READ = "memory:read"
SCOPE_WRITE = "memory:write"
SCOPE_ADMIN = "memory:admin"

_UNIFORM_NOT_AUTHORIZED = "not authorized"

#: Task 3.8 / design 9.1 — an exact count never leaves the server, and a
#: bucket is only disclosed once the scope is large enough that the bucket
#: itself cannot identify a specific memory.
MIN_DIAGNOSTIC_CARDINALITY = 10
BUCKET_SUPPRESSED = "suppressed"
BUCKET_SMALL = "10-99"
BUCKET_LARGE = "100+"
DIAGNOSTIC_BUCKETS: tuple[str, ...] = (BUCKET_SUPPRESSED, BUCKET_SMALL, BUCKET_LARGE)


def cardinality_bucket(count: int) -> str:
    """Map an exact count to a disclosure-safe bucket label."""

    if count < MIN_DIAGNOSTIC_CARDINALITY:
        return BUCKET_SUPPRESSED
    if count < 100:
        return BUCKET_SMALL
    return BUCKET_LARGE


@dataclass(frozen=True)
class CallerContext:
    """Identity derived from the verified connection, never from a payload."""

    grant_id: str
    owner_id: str
    actor_id: str
    source_client: str
    oauth_scopes: tuple[str, ...] = ()
    memory_scopes: tuple[str, ...] = ()

    def has_oauth_scope(self, required: str) -> bool:
        return required in self.oauth_scopes or SCOPE_ADMIN in self.oauth_scopes

    def may_reach(self, scope: str) -> bool:
        return scope in self.memory_scopes


class _Denied(Exception):
    """Internal control flow for a pre-repository denial."""

    def __init__(self, code: models.ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _default_id_factory() -> str:
    return uuid.uuid4().hex


@dataclass
class RecallMemoryService:
    """Pure application layer shared by every transport and by the CLI."""

    repository: repo.MemoryRepository
    embedder: repo.Embedder
    digest_key: bytes
    server_version: str
    clock: Callable[[], str]
    id_factory: Callable[[], str] = field(default=_default_id_factory)

    # -- helpers --------------------------------------------------------
    def _meta(self, request_id: str, *, degraded: bool = False) -> models.ToolMeta:
        return models.ToolMeta(
            server_version=self.server_version, degraded=degraded, request_id=request_id
        )

    def _ok(
        self, request_id: str, data: dict[str, Any], *, degraded: bool = False
    ) -> models.ToolEnvelope:
        return models.ToolEnvelope(data=data, meta=self._meta(request_id, degraded=degraded))

    def _fail(
        self,
        request_id: str,
        code: models.ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        degraded: bool = False,
    ) -> models.ToolErrorEnvelope:
        return models.ToolErrorEnvelope(
            error=models.ToolError(code=code, message=message, details=details or {}),
            meta=self._meta(request_id, degraded=degraded),
        )

    def _authorize(self, ctx: CallerContext, oauth_scope: str, *memory_scopes: str) -> None:
        if not ctx.has_oauth_scope(oauth_scope):
            raise _Denied(models.ErrorCode.INSUFFICIENT_SCOPE, f"{oauth_scope} is required")
        for scope in memory_scopes:
            if not ctx.may_reach(scope):
                raise _Denied(models.ErrorCode.NOT_AUTHORIZED, _UNIFORM_NOT_AUTHORIZED)

    def _digest(self, operation: str, payload: dict[str, Any]) -> str:
        """Keyed digest of the caller-visible payload (R8.3).

        Server-generated identifiers and timestamps are excluded so the same
        logical write always produces the same digest.
        """

        canonical = json.dumps(
            {"operation": operation, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self.digest_key, canonical, hashlib.sha256).hexdigest()

    def _embed(self, text: str) -> tuple[dict[int, bytes] | None, bool]:
        try:
            return self.embedder.encode(text), False
        except repo.EmbeddingUnavailableError:
            return None, True
        except Exception as exc:  # noqa: BLE001 - degradation must never crash a read
            logger.warning("embedder failed: %s", redaction.redact_exception(exc))
            return None, True

    def _to_result(self, row: Any) -> models.MemoryResult:
        return models.MemoryResult(
            memory_id=row.memory_id,
            revision=row.revision,
            content=row.content,
            scope=row.scope,
            kind=row.kind,
            tags=tuple(getattr(row, "tags", ()) or ()),
            score=getattr(row, "score", None),
            provenance=models.Provenance(
                source_client=row.source_client,
                source_conversation=getattr(row, "source_conversation", None),
                created_at=row.created_at,
                updated_at=row.updated_at,
            ),
        )

    def _map_error(
        self, request_id: str, exc: Exception, *, degraded: bool = False
    ) -> models.ToolErrorEnvelope:
        if isinstance(exc, (repo.NotAuthorizedError, PermissionError)):
            return self._fail(
                request_id,
                models.ErrorCode.NOT_AUTHORIZED,
                _UNIFORM_NOT_AUTHORIZED,
                degraded=degraded,
            )
        if isinstance(exc, repo.RevisionConflictError):
            return self._fail(
                request_id,
                models.ErrorCode.REVISION_CONFLICT,
                "revision conflict",
                details={"current_revision": exc.current_revision},
                degraded=degraded,
            )
        if isinstance(exc, repo.IdempotencyKeyReusedError):
            return self._fail(
                request_id,
                models.ErrorCode.IDEMPOTENCY_KEY_REUSED,
                "idempotency key reused with a different payload",
                degraded=degraded,
            )
        if isinstance(exc, repo.IdempotencyKeyPurgedError):
            return self._fail(
                request_id,
                models.ErrorCode.IDEMPOTENCY_KEY_PURGED,
                "idempotency key was purged",
                degraded=degraded,
            )
        if isinstance(exc, repo.EmbeddingUnavailableError):
            return self._fail(
                request_id,
                models.ErrorCode.EMBEDDING_UNAVAILABLE,
                "embedding backend unavailable",
                degraded=True,
            )
        if isinstance(exc, (repo.RepositoryValidationError, ValueError)):
            return self._fail(
                request_id,
                models.ErrorCode.VALIDATION_ERROR,
                "invalid argument",
                degraded=degraded,
            )
        # R8.5 — the wire message stays generic; the redacted detail is logged.
        logger.error("unhandled repository failure: %s", redaction.redact_exception(exc))
        return self._fail(
            request_id, models.ErrorCode.INTERNAL_ERROR, "internal error", degraded=degraded
        )

    # -- read tools -----------------------------------------------------
    def search(
        self, ctx: CallerContext, payload: models.SearchRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        try:
            self._authorize(ctx, SCOPE_READ, payload.scope)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        # Probing the embedder tells us whether this search is vector-backed
        # or degraded to keyword/FTS only (R6.4).
        _, degraded = self._embed(payload.query)
        try:
            rows: Sequence[Any] = self.repository.search_scoped(
                grant_id=ctx.grant_id,
                scope=payload.scope,
                query=payload.query,
                limit=payload.limit,
            )
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc, degraded=degraded)

        memories = [self._to_result(row).model_dump() for row in rows]
        return self._ok(request_id, {"memories": memories}, degraded=degraded)

    def get(
        self, ctx: CallerContext, payload: models.GetRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        try:
            self._authorize(ctx, SCOPE_READ, payload.scope)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        try:
            row = self.repository.get_scoped_memory(
                grant_id=ctx.grant_id, scope=payload.scope, memory_id=payload.memory_id
            )
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc)

        if row is None:
            return self._fail(request_id, models.ErrorCode.NOT_FOUND, "memory not found")
        return self._ok(request_id, {"memory": self._to_result(row).model_dump()})

    def recent(
        self, ctx: CallerContext, payload: models.RecentRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        try:
            self._authorize(ctx, SCOPE_READ, payload.scope)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        try:
            rows: Sequence[Any] = self.repository.recent_scoped_memories(
                grant_id=ctx.grant_id, scope=payload.scope, limit=payload.limit
            )
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc)

        return self._ok(
            request_id, {"memories": [self._to_result(row).model_dump() for row in rows]}
        )

    def status(
        self, ctx: CallerContext, payload: models.StatusRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        try:
            self._authorize(ctx, SCOPE_READ)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        try:
            health = self.repository.health()
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc)

        _, degraded = self._embed("healthcheck")
        # No exact cardinality, no filesystem location (R3.8 / R8.5).
        data = {
            "server_version": self.server_version,
            "transport": {"stateful_http": True},
            "database": {
                "reachable": health.reachable,
                "schema_version": health.schema_version,
            },
            "embedding": {
                "available": not degraded,
                "generation": health.embedding_generation,
            },
            "authorized_scopes": list(ctx.memory_scopes),
        }

        if getattr(payload, "include_diagnostics", False):
            # R3.8 — diagnostics are owner-admin only and never exact.
            if SCOPE_ADMIN not in ctx.oauth_scopes:
                return self._fail(
                    request_id,
                    models.ErrorCode.INSUFFICIENT_SCOPE,
                    f"{SCOPE_ADMIN} is required",
                    degraded=degraded,
                )
            data["diagnostics"] = {
                "cardinality_buckets": self._cardinality_buckets(ctx),
                "bucket_threshold": MIN_DIAGNOSTIC_CARDINALITY,
            }

        return self._ok(request_id, data, degraded=degraded)

    def _cardinality_buckets(self, ctx: CallerContext) -> dict[str, str]:
        counter = getattr(self.repository, "scope_cardinality", None)
        buckets: dict[str, str] = {}
        for scope in ctx.memory_scopes:
            if counter is None:
                buckets[scope] = BUCKET_SUPPRESSED
                continue
            try:
                count = counter(grant_id=ctx.grant_id, scope=scope)
            except Exception as exc:  # noqa: BLE001 - diagnostics never crash status
                logger.warning(
                    "cardinality probe failed: %s", redaction.redact_exception(exc)
                )
                buckets[scope] = BUCKET_SUPPRESSED
                continue
            buckets[scope] = cardinality_bucket(int(count))
        return buckets

    # -- write tools ----------------------------------------------------
    def add(
        self, ctx: CallerContext, payload: models.AddRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        try:
            self._authorize(ctx, SCOPE_WRITE, payload.scope)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        blobs, degraded = self._embed(payload.content)
        if blobs is None:
            return self._fail(
                request_id,
                models.ErrorCode.EMBEDDING_UNAVAILABLE,
                "embedding backend unavailable",
                degraded=True,
            )

        digest = self._digest(
            "add",
            {
                "content": payload.content,
                "scope": payload.scope,
                "kind": payload.kind,
                "tags": list(payload.tags),
                "source_conversation": payload.source_conversation,
                "idempotency_key": payload.idempotency_key,
            },
        )
        try:
            outcome = self.repository.add_memory(
                grant_id=ctx.grant_id,
                scope=payload.scope,
                memory_id=self.id_factory(),
                content=payload.content,
                kind=payload.kind,
                tags=payload.tags,
                source_conversation=payload.source_conversation,
                actor_id=ctx.actor_id,
                idempotency_key=payload.idempotency_key,
                payload_digest=digest,
                occurred_at=self.clock(),
                embedding_blobs=blobs,
            )
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc, degraded=degraded)

        return self._ok(
            request_id,
            {
                "memory_id": outcome.memory_id,
                "revision": outcome.revision,
                "created_at": outcome.created_at,
            },
            degraded=degraded,
        )

    def replace(
        self, ctx: CallerContext, payload: models.ReplaceRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        target_scopes = [payload.scope]
        if payload.new_scope is not None:
            target_scopes.append(payload.new_scope)
        try:
            self._authorize(ctx, SCOPE_WRITE, *target_scopes)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        blobs, degraded = self._embed(payload.content)
        if blobs is None:
            return self._fail(
                request_id,
                models.ErrorCode.EMBEDDING_UNAVAILABLE,
                "embedding backend unavailable",
                degraded=True,
            )

        digest = self._digest(
            "replace",
            {
                "memory_id": payload.memory_id,
                "expected_revision": payload.expected_revision,
                "content": payload.content,
                "scope": payload.scope,
                "new_scope": payload.new_scope,
                "kind": payload.kind,
                "tags": list(payload.tags),
                "source_conversation": payload.source_conversation,
                "idempotency_key": payload.idempotency_key,
            },
        )
        try:
            outcome = self.repository.replace_memory(
                grant_id=ctx.grant_id,
                scope=payload.scope,
                memory_id=payload.memory_id,
                expected_revision=payload.expected_revision,
                content=payload.content,
                kind=payload.kind,
                tags=payload.tags,
                source_conversation=payload.source_conversation,
                actor_id=ctx.actor_id,
                idempotency_key=payload.idempotency_key,
                payload_digest=digest,
                occurred_at=self.clock(),
                embedding_blobs=blobs,
                new_scope=payload.new_scope,
            )
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc, degraded=degraded)

        return self._ok(
            request_id,
            {
                "memory_id": outcome.memory_id,
                "revision": outcome.revision,
                "updated_at": outcome.updated_at,
            },
            degraded=degraded,
        )

    def remove(
        self, ctx: CallerContext, payload: models.RemoveRequest
    ) -> models.ToolEnvelope | models.ToolErrorEnvelope:
        request_id = self.id_factory()
        try:
            self._authorize(ctx, SCOPE_WRITE, payload.scope)
        except _Denied as denied:
            return self._fail(request_id, denied.code, denied.message)

        digest = self._digest(
            "remove",
            {
                "memory_id": payload.memory_id,
                "expected_revision": payload.expected_revision,
                "scope": payload.scope,
                "idempotency_key": payload.idempotency_key,
            },
        )
        try:
            outcome = self.repository.remove_memory(
                grant_id=ctx.grant_id,
                scope=payload.scope,
                memory_id=payload.memory_id,
                expected_revision=payload.expected_revision,
                actor_id=ctx.actor_id,
                idempotency_key=payload.idempotency_key,
                payload_digest=digest,
                occurred_at=self.clock(),
            )
        except Exception as exc:  # noqa: BLE001
            return self._map_error(request_id, exc)

        return self._ok(
            request_id,
            {
                "memory_id": outcome.memory_id,
                "revision": outcome.revision,
                "deleted_at": outcome.deleted_at,
                "deleted": True,
            },
        )


__all__ = [
    "BUCKET_LARGE",
    "BUCKET_SMALL",
    "BUCKET_SUPPRESSED",
    "DIAGNOSTIC_BUCKETS",
    "MIN_DIAGNOSTIC_CARDINALITY",
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "CallerContext",
    "RecallMemoryService",
    "cardinality_bucket",
]
