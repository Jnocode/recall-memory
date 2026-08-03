"""Repository + embedder protocols (support for task 2.7).

``service.py`` may only depend on *this* module — never on a concrete
storage implementation and never on an MCP transport.  The recall-sqlite
adapter (later phase) implements these protocols and translates
``recall.mcp_repository`` exceptions into the taxonomy defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# error taxonomy
# --------------------------------------------------------------------------


class RepositoryError(Exception):
    """Base class for every repository failure the service can classify."""


class NotAuthorizedError(RepositoryError):
    """Uniform, content-free authorization failure.

    Never distinguishes "does not exist" from "not yours": that difference is
    itself a disclosure (R7.8).
    """

    def __init__(self, message: str = "not authorized") -> None:
        super().__init__(message)


class RepositoryValidationError(RepositoryError):
    """The repository rejected the arguments."""


class RevisionConflictError(RepositoryError):
    """``expected_revision`` did not match the stored row (R5.3)."""

    def __init__(self, *, current_revision: int) -> None:
        super().__init__("revision conflict")
        self.current_revision = current_revision


class IdempotencyKeyReusedError(RepositoryError):
    """Same key, different payload (R5.2). The message stays content-free."""

    def __init__(self) -> None:
        super().__init__("idempotency key reused with a different payload")


class IdempotencyKeyPurgedError(RepositoryError):
    """The key belongs to a hard-purged memory (R5.8)."""

    def __init__(self) -> None:
        super().__init__("idempotency key was purged")


class EmbeddingUnavailableError(RepositoryError):
    """No embedding could be produced for this text (R6.4)."""


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AddOutcome:
    memory_id: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class ReplaceOutcome:
    memory_id: str
    revision: int
    updated_at: str


@dataclass(frozen=True)
class RemoveOutcome:
    memory_id: str
    revision: int
    deleted_at: str


@dataclass(frozen=True)
class RepositoryHealth:
    reachable: bool
    schema_version: int
    embedding_generation: int | None = None


# --------------------------------------------------------------------------
# protocols
# --------------------------------------------------------------------------


@runtime_checkable
class MemoryRow(Protocol):
    """Structural view of one stored memory as returned by reads."""

    memory_id: str
    content: str
    revision: int
    scope: str
    kind: str
    source_client: str
    source_conversation: str | None
    created_at: str
    updated_at: str


@runtime_checkable
class MemoryRepository(Protocol):
    """Every method authorizes on ``grant_id`` + exact ``scope`` in SQL."""

    def search_scoped(self, **kwargs: Any) -> tuple[Any, ...]: ...

    def get_scoped_memory(self, **kwargs: Any) -> Any | None: ...

    def recent_scoped_memories(self, **kwargs: Any) -> tuple[Any, ...]: ...

    def add_memory(self, **kwargs: Any) -> AddOutcome: ...

    def replace_memory(self, **kwargs: Any) -> ReplaceOutcome: ...

    def remove_memory(self, **kwargs: Any) -> RemoveOutcome: ...

    def health(self) -> RepositoryHealth: ...


@runtime_checkable
class Embedder(Protocol):
    """Maps text to one blob per non-retired embedding generation."""

    def encode(self, text: str) -> dict[int, bytes]: ...


__all__ = [
    "AddOutcome",
    "Embedder",
    "EmbeddingUnavailableError",
    "IdempotencyKeyPurgedError",
    "IdempotencyKeyReusedError",
    "MemoryRepository",
    "MemoryRow",
    "NotAuthorizedError",
    "RemoveOutcome",
    "ReplaceOutcome",
    "RepositoryError",
    "RepositoryHealth",
    "RepositoryValidationError",
    "RevisionConflictError",
]
