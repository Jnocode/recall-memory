"""Owner-scoped online admin operations (tasks 6.10 / 6.10a / 6.10b).

``memory export`` / ``memory restore`` / ``memory purge`` are **not** model
tools and they are **not** offline break-glass commands.  They are owner-
scoped operations that only ever run *inside the live authority process*:

* The owner is always derived here, server side, from the verified caller
  context (an OS-bound local admin session or a ``memory:admin`` grant).
  There is no ``--owner-id`` anywhere in this module's API, so a caller can
  never name an owner it does not already prove (R8.10).
* Every operation re-checks ``memory:admin`` **and** exact memory-scope
  reachability before touching the repository, and the repository re-checks
  the same grant inside its own SQL transaction.  Two independent checks,
  neither of which trusts the transport.
* Failures are uniform: "not authorized" never distinguishes "no such
  memory" from "not yours" (R7.8).
* Every admin operation appends a **content-free** audit record.  The audit
  stores keyed digests and identifiers only; :mod:`breakglass` already
  enforces the "no paths, no content" rule, and this module reuses that
  enforcement rather than inventing a second one.

The purge path is deliberately thin: the irreversible work happens in one
``BEGIN IMMEDIATE`` transaction inside the canonical core repository.  This
module only supplies the two digests the core needs (the audit digest for
the final purge event, and the *deny* digest that replaces every scrubbed
idempotency payload) and refuses to proceed if the caller is not authorized.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from . import breakglass, redaction
from . import repository as repo
from .service import SCOPE_ADMIN, CallerContext

logger = logging.getLogger("recall_memory_mcp.admin")

#: Sidecar audit for *online* owner-scoped admin activity.  Deliberately a
#: different file from the offline operator audit: mixing a trusted-OS-
#: operator trail with an owner-ACL trail would make both unreadable.
ADMIN_AUDIT_SUFFIX: Final[str] = ".admin-audit.jsonl"

EXPORT_SCHEMA_VERSION: Final[int] = 1

#: Printed by the CLI and embedded in every export document (R8.8).
BACKUP_RETENTION_NOTICE: Final[str] = (
    "This export contains plaintext memory content. A later `memory purge` "
    "removes content from the authority database ONLY. It cannot reach this "
    "file, any copy of it, or any whole-database backup. Retention and "
    "destruction of those copies is the operator's responsibility."
)


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------


class AdminError(RuntimeError):
    """Base class for every admin-operation failure."""


class AdminNotAuthorizedError(AdminError):
    """Uniform, content-free authorization failure (R7.8)."""

    def __init__(self, message: str = "not authorized") -> None:
        super().__init__(message)


class AdminValidationError(AdminError):
    """The arguments were rejected before anything was touched."""


class AdminRevisionConflictError(AdminError):
    """``expected_revision`` did not match the stored revision."""

    def __init__(self, *, current_revision: int) -> None:
        super().__init__("revision conflict")
        self.current_revision = current_revision


class AdminUnavailableError(AdminError):
    """The authority could not serve the request (never a leak of *why*)."""


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def admin_audit_path_for(db_path: str | Path) -> Path:
    """Sidecar admin-audit path for ``db_path``."""

    path = Path(db_path).expanduser()
    return path.with_name(path.name + ADMIN_AUDIT_SUFFIX)


class AdminAuditLog(breakglass.OperatorAuditLog):
    """Append-only content-free JSONL audit of online admin activity.

    Subclassed from the break-glass log purely to inherit its *enforcement*:
    :func:`breakglass._assert_content_free` refuses any value that looks like
    a filesystem path, and this class never passes content in.
    """


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportedMemory:
    memory_id: str
    revision: int
    scope: str
    kind: str
    content: str
    tags: tuple[str, ...]
    content_hash: str
    source_client: str
    source_conversation: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
            "kind": self.kind,
            "memory_id": self.memory_id,
            "revision": self.revision,
            "scope": self.scope,
            "source_client": self.source_client,
            "source_conversation": self.source_conversation,
            "tags": list(self.tags),
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ExportResult:
    scope: str
    include_tombstones: bool
    memories: tuple[ExportedMemory, ...]
    exported_at: str
    audit_event_id: str

    @property
    def tombstone_count(self) -> int:
        return sum(1 for memory in self.memories if memory.deleted_at is not None)

    def document(self) -> dict[str, Any]:
        """The exact JSON document the CLI writes to disk."""

        return {
            "backup_retention_notice": BACKUP_RETENTION_NOTICE,
            "exported_at": self.exported_at,
            "includes_tombstones": self.include_tombstones,
            "memories": [memory.as_json() for memory in self.memories],
            "memory_count": len(self.memories),
            "schema_version": EXPORT_SCHEMA_VERSION,
            "scope": self.scope,
            "tombstone_count": self.tombstone_count,
        }


@dataclass(frozen=True)
class RestoreResult:
    memory_id: str
    revision: int
    restored_at: str
    audit_event_id: str

    def as_json(self) -> dict[str, Any]:
        return {
            "audit_event_id": self.audit_event_id,
            "memory_id": self.memory_id,
            "restored_at": self.restored_at,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class PurgeResult:
    memory_id: str
    final_revision: int
    purged_at: str
    audit_event_id: str

    def as_json(self) -> dict[str, Any]:
        return {
            "audit_event_id": self.audit_event_id,
            "final_revision": self.final_revision,
            "memory_id": self.memory_id,
            "purged_at": self.purged_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


@dataclass
class AdminService:
    """Transport-free owner-scoped admin operations.

    ``repository`` must expose ``export_scoped``, ``restore_memory`` and
    ``purge_memory``; :class:`~recall_memory_mcp.authority.SqliteAuthorityRepository`
    does, and every one of those re-authorizes the grant in SQL.
    """

    repository: Any
    digest_key: bytes
    db_identity: str
    audit: AdminAuditLog | None = None
    clock: Callable[[], str] = _utc_now
    _seen_keys: set[str] = field(default_factory=set, repr=False)

    # -- helpers --------------------------------------------------------
    def _authorize(self, ctx: CallerContext, scope: str) -> None:
        """``memory:admin`` **and** exact scope reachability, or nothing.

        Note the deliberate asymmetry with the model tools: there is no
        "insufficient scope" variant here.  An admin CLI caller either is the
        owner-admin for this scope or gets one uniform refusal, so probing
        with a downgraded grant cannot map out which scopes exist.
        """

        if not isinstance(ctx, CallerContext):  # pragma: no cover - defensive
            raise AdminNotAuthorizedError()
        if not ctx.has_oauth_scope(SCOPE_ADMIN):
            raise AdminNotAuthorizedError()
        if not ctx.may_reach(scope):
            raise AdminNotAuthorizedError()

    @staticmethod
    def _require_text(name: str, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AdminValidationError(f"{name} must not be blank")
        return value

    @staticmethod
    def _require_revision(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AdminValidationError("expected_revision must be a positive integer")
        return value

    def _digest(self, operation: str, payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            {"operation": operation, "payload": dict(payload)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self.digest_key, canonical, hashlib.sha256).hexdigest()

    def _keyed_id(self, value: str) -> str:
        """Keyed, non-dictionary-invertible identifier for the audit trail."""

        return hmac.new(self.digest_key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _record(
        self, *, operation: str, outcome: str, detail: Mapping[str, Any]
    ) -> str:
        if self.audit is None:
            return ""
        try:
            entry = self.audit.append(
                operation=operation,
                outcome=outcome,
                db_identity=self.db_identity,
                detail=dict(detail),
            )
        except Exception as exc:  # noqa: BLE001 - an audit failure must be visible
            logger.error("admin audit write failed: %s", redaction.redact_exception(exc))
            raise AdminUnavailableError("the admin audit trail could not be written") from exc
        return entry.event_id

    def _translate(self, exc: BaseException) -> BaseException:
        if isinstance(exc, AdminError):
            return exc
        if isinstance(exc, (repo.NotAuthorizedError, PermissionError)):
            return AdminNotAuthorizedError()
        if isinstance(exc, repo.RevisionConflictError):
            return AdminRevisionConflictError(current_revision=int(exc.current_revision))
        if isinstance(exc, (repo.RepositoryValidationError, ValueError)):
            return AdminValidationError("invalid argument")
        logger.error("admin operation failed: %s", redaction.redact_exception(exc))
        return AdminUnavailableError("the admin operation could not be completed")

    # -- operations ------------------------------------------------------
    def export(
        self,
        ctx: CallerContext,
        *,
        scope: str,
        include_tombstones: bool = False,
    ) -> ExportResult:
        """Every memory in ONE authorized scope of the caller's own owner."""

        self._require_text("scope", scope)
        self._authorize(ctx, scope)
        if not isinstance(include_tombstones, bool):
            raise AdminValidationError("include_tombstones must be a boolean")

        try:
            rows = self.repository.export_scoped(
                grant_id=ctx.grant_id,
                scope=scope,
                include_tombstones=include_tombstones,
            )
        except BaseException as exc:  # noqa: BLE001
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc

        memories = tuple(
            ExportedMemory(
                memory_id=str(row["memory_id"]),
                revision=int(row["revision"]),
                scope=str(row["scope"]),
                kind=str(row["kind"]),
                content=str(row["content"]),
                tags=tuple(row.get("tags") or ()),
                content_hash=str(row["content_hash"]),
                source_client=str(row["source_client"]),
                source_conversation=row.get("source_conversation"),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                deleted_at=row.get("deleted_at"),
            )
            for row in rows
        )
        exported_at = self.clock()
        event_id = self._record(
            operation="memory_export",
            outcome="ok",
            detail={
                "include_tombstones": include_tombstones,
                "memory_count": len(memories),
                "owner_ref": self._keyed_id(ctx.owner_id),
                "scope": scope,
                "tombstone_count": sum(1 for m in memories if m.deleted_at is not None),
            },
        )
        return ExportResult(
            scope=scope,
            include_tombstones=include_tombstones,
            memories=memories,
            exported_at=exported_at,
            audit_event_id=event_id,
        )

    def restore(
        self,
        ctx: CallerContext,
        *,
        memory_id: str,
        scope: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> RestoreResult:
        """Bring one soft-deleted memory back with a fresh revision."""

        self._require_text("memory_id", memory_id)
        self._require_text("scope", scope)
        self._require_text("idempotency_key", idempotency_key)
        revision = self._require_revision(expected_revision)
        self._authorize(ctx, scope)

        occurred_at = self.clock()
        payload_digest = self._digest(
            "restore",
            {
                "expected_revision": revision,
                "idempotency_key": idempotency_key,
                "memory_id": memory_id,
                "scope": scope,
            },
        )
        try:
            outcome = self.repository.restore_memory(
                grant_id=ctx.grant_id,
                scope=scope,
                memory_id=memory_id,
                expected_revision=revision,
                actor_id=ctx.actor_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                occurred_at=occurred_at,
            )
        except BaseException as exc:  # noqa: BLE001
            translated = self._translate(exc)
            self._record(
                operation="memory_restore",
                outcome="refused",
                detail={
                    "memory_ref": self._keyed_id(memory_id),
                    "owner_ref": self._keyed_id(ctx.owner_id),
                    "reason": type(translated).__name__,
                    "scope": scope,
                },
            )
            if translated is exc:
                raise
            raise translated from exc

        event_id = self._record(
            operation="memory_restore",
            outcome="ok",
            detail={
                "memory_ref": self._keyed_id(memory_id),
                "owner_ref": self._keyed_id(ctx.owner_id),
                "revision": int(outcome.revision),
                "scope": scope,
            },
        )
        return RestoreResult(
            memory_id=str(outcome.memory_id),
            revision=int(outcome.revision),
            restored_at=str(outcome.restored_at),
            audit_event_id=event_id,
        )

    def purge(
        self,
        ctx: CallerContext,
        *,
        memory_id: str,
        scope: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> PurgeResult:
        """Irreversibly remove one memory's content and every index of it.

        The *deny* digest handed to the core is keyed and derived only from
        identifiers — never from content — so the scrubbed idempotency rows
        that survive can prove "this key belonged to a purged memory" without
        ever being able to reconstruct what was purged (R8.9).
        """

        self._require_text("memory_id", memory_id)
        self._require_text("scope", scope)
        self._require_text("idempotency_key", idempotency_key)
        revision = self._require_revision(expected_revision)
        self._authorize(ctx, scope)

        occurred_at = self.clock()
        payload_digest = self._digest(
            "purge",
            {
                "expected_revision": revision,
                "idempotency_key": idempotency_key,
                "memory_id": memory_id,
                "scope": scope,
            },
        )
        deny_payload_digest = self._digest(
            "purge-deny", {"memory_id": memory_id, "scope": scope}
        )
        try:
            outcome = self.repository.purge_memory(
                grant_id=ctx.grant_id,
                scope=scope,
                memory_id=memory_id,
                expected_revision=revision,
                actor_id=ctx.actor_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                deny_payload_digest=deny_payload_digest,
                occurred_at=occurred_at,
            )
        except BaseException as exc:  # noqa: BLE001
            translated = self._translate(exc)
            self._record(
                operation="memory_purge",
                outcome="refused",
                detail={
                    "memory_ref": self._keyed_id(memory_id),
                    "owner_ref": self._keyed_id(ctx.owner_id),
                    "reason": type(translated).__name__,
                    "scope": scope,
                },
            )
            if translated is exc:
                raise
            raise translated from exc

        event_id = self._record(
            operation="memory_purge",
            outcome="ok",
            detail={
                "final_revision": int(outcome.final_revision),
                "irreversible": True,
                "memory_ref": self._keyed_id(memory_id),
                "owner_ref": self._keyed_id(ctx.owner_id),
                "scope": scope,
            },
        )
        return PurgeResult(
            memory_id=str(outcome.memory_id),
            final_revision=int(outcome.final_revision),
            purged_at=str(outcome.purged_at),
            audit_event_id=event_id,
        )


def build_admin_service(
    settings: Any,
    *,
    repository: Any,
    digest_key: bytes,
    clock: Callable[[], str] | None = None,
) -> AdminService:
    """Wire an :class:`AdminService` onto a live authority database."""

    db_path = Path(settings.db_path)
    return AdminService(
        repository=repository,
        digest_key=digest_key,
        db_identity=breakglass.db_identity(db_path),
        audit=AdminAuditLog(admin_audit_path_for(db_path)),
        clock=clock or _utc_now,
    )


def scopes_of(ctx: CallerContext) -> Sequence[str]:
    """Memory scopes the caller may administer (never widened here)."""

    return tuple(ctx.memory_scopes)


__all__ = [
    "ADMIN_AUDIT_SUFFIX",
    "BACKUP_RETENTION_NOTICE",
    "EXPORT_SCHEMA_VERSION",
    "AdminAuditLog",
    "AdminError",
    "AdminNotAuthorizedError",
    "AdminRevisionConflictError",
    "AdminService",
    "AdminUnavailableError",
    "AdminValidationError",
    "ExportResult",
    "ExportedMemory",
    "PurgeResult",
    "RestoreResult",
    "admin_audit_path_for",
    "build_admin_service",
    "scopes_of",
]
