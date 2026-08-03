"""Strict request/result types (task 2.4).

Design section 7 defines the wire envelope; R6.3 requires explicit bounds on
`limit`, content, tags and scope; R4 forbids any client-supplied identity.

Every request model sets ``extra="forbid"`` so a host that tries to smuggle
`owner_id`, `actor_id`, `source_client` or `revision` into the payload fails
validation instead of being silently trusted.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# documented bounds (R6.3)
# --------------------------------------------------------------------------

MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

MAX_QUERY_CHARS = 1024
MAX_CONTENT_CHARS = 20_000
MAX_SCOPE_CHARS = 128
MAX_KIND_CHARS = 32
MAX_TAGS = 16
MAX_TAG_CHARS = 64
MAX_MEMORY_ID_CHARS = 128
MAX_SOURCE_CONVERSATION_CHARS = 128
MIN_IDEMPOTENCY_KEY_CHARS = 8
MAX_IDEMPOTENCY_KEY_CHARS = 128

#: `global` or `project:<slug>`. `legacy:unscoped` is intentionally excluded:
#: legacy rows are migration-only and must not be addressable by a client.
SCOPE_PATTERN = r"^(?:global|project:[a-z0-9][a-z0-9._-]{0,62})$"
KIND_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
TAG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
MEMORY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

DATA_TRUST_UNTRUSTED = "untrusted_memory_content"


class ErrorCode(str, Enum):
    """Stable, content-free error codes returned to every host."""

    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    NOT_AUTHORIZED = "not_authorized"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    IDEMPOTENCY_KEY_PURGED = "idempotency_key_purged"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


# --------------------------------------------------------------------------
# shared field types
# --------------------------------------------------------------------------

Scope = Annotated[str, Field(max_length=MAX_SCOPE_CHARS, pattern=SCOPE_PATTERN)]
Kind = Annotated[str, Field(max_length=MAX_KIND_CHARS, pattern=KIND_PATTERN)]
MemoryId = Annotated[str, Field(max_length=MAX_MEMORY_ID_CHARS, pattern=MEMORY_ID_PATTERN)]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=MIN_IDEMPOTENCY_KEY_CHARS,
        max_length=MAX_IDEMPOTENCY_KEY_CHARS,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
]
Tag = Annotated[str, Field(max_length=MAX_TAG_CHARS, pattern=TAG_PATTERN)]
Tags = Annotated[tuple[Tag, ...], Field(max_length=MAX_TAGS)]


def _require_strict_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


class _StrictModel(BaseModel):
    """Base for every request: unknown fields are rejected, values frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class _RequestWithLimit(_StrictModel):
    @field_validator("limit", mode="before", check_fields=False)
    @classmethod
    def _clamp_limit(cls, value: Any) -> int:
        value = _require_strict_int(value, "limit")
        return max(MIN_LIMIT, min(MAX_LIMIT, value))


class _RequestWithExpectedRevision(_StrictModel):
    @field_validator("expected_revision", mode="before", check_fields=False)
    @classmethod
    def _strict_expected_revision(cls, value: Any) -> int:
        return _require_strict_int(value, "expected_revision")


class _RequestWithContent(_StrictModel):
    @field_validator("content", check_fields=False)
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator("tags", mode="before", check_fields=False)
    @classmethod
    def _normalise_tags(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("tags must be a list of strings")
        seen: list[str] = []
        for tag in value:
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError("tags must be non-blank strings")
            if tag not in seen:
                seen.append(tag)
        return tuple(seen)


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------


class SearchRequest(_RequestWithLimit):
    query: Annotated[str, Field(max_length=MAX_QUERY_CHARS)]
    scope: Scope
    limit: int = DEFAULT_LIMIT

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        # R6.2 — a blank query is a validation error, never a recent dump.
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class GetRequest(_StrictModel):
    memory_id: MemoryId
    scope: Scope


class RecentRequest(_RequestWithLimit):
    scope: Scope
    limit: int = DEFAULT_LIMIT


class AddRequest(_RequestWithContent):
    content: Annotated[str, Field(max_length=MAX_CONTENT_CHARS)]
    scope: Scope
    kind: Kind
    tags: Tags = ()
    source_conversation: Annotated[
        str | None, Field(max_length=MAX_SOURCE_CONVERSATION_CHARS)
    ] = None
    idempotency_key: IdempotencyKey


class ReplaceRequest(_RequestWithContent, _RequestWithExpectedRevision):
    memory_id: MemoryId
    expected_revision: Annotated[int, Field(ge=1)]
    content: Annotated[str, Field(max_length=MAX_CONTENT_CHARS)]
    scope: Scope
    kind: Kind
    tags: Tags = ()
    source_conversation: Annotated[
        str | None, Field(max_length=MAX_SOURCE_CONVERSATION_CHARS)
    ] = None
    new_scope: Scope | None = None
    idempotency_key: IdempotencyKey


class RemoveRequest(_RequestWithExpectedRevision):
    memory_id: MemoryId
    scope: Scope
    expected_revision: Annotated[int, Field(ge=1)]
    idempotency_key: IdempotencyKey


class StatusRequest(_StrictModel):
    """Status must not become a filtered dump surface (R3.8, design 9.1).

    The single input is a boolean: there is deliberately no scope/query
    filter, so status cannot be used to probe for the existence of content.
    ``include_diagnostics`` additionally requires the ``memory:admin`` scope
    and still only ever answers with coarse cardinality buckets.
    """

    include_diagnostics: bool = False

    @field_validator("include_diagnostics", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("include_diagnostics must be a boolean")
        return value


REQUEST_MODELS: tuple[str, ...] = (
    "SearchRequest",
    "GetRequest",
    "RecentRequest",
    "AddRequest",
    "ReplaceRequest",
    "RemoveRequest",
    "StatusRequest",
)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


class _ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(_ResultModel):
    source_client: str
    source_conversation: str | None = None
    created_at: str
    updated_at: str


class MemoryResult(_ResultModel):
    memory_id: str
    revision: int
    content: str
    scope: str
    kind: str
    tags: tuple[str, ...] = ()
    score: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    provenance: Provenance
    # R8.1 — stored content is data, never instructions.
    data_trust: Literal["untrusted_memory_content"] = DATA_TRUST_UNTRUSTED


class ToolMeta(_ResultModel):
    server_version: str
    degraded: bool = False
    request_id: str


class ToolError(_ResultModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolEnvelope(_ResultModel):
    ok: Literal[True] = True
    data: dict[str, Any]
    meta: ToolMeta


class ToolErrorEnvelope(_ResultModel):
    ok: Literal[False] = False
    error: ToolError
    meta: ToolMeta


__all__ = [
    "DATA_TRUST_UNTRUSTED",
    "DEFAULT_LIMIT",
    "IDEMPOTENCY_KEY_PATTERN",
    "KIND_PATTERN",
    "MAX_CONTENT_CHARS",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_KIND_CHARS",
    "MAX_LIMIT",
    "MAX_MEMORY_ID_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_SCOPE_CHARS",
    "MAX_SOURCE_CONVERSATION_CHARS",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "MIN_IDEMPOTENCY_KEY_CHARS",
    "MIN_LIMIT",
    "REQUEST_MODELS",
    "SCOPE_PATTERN",
    "AddRequest",
    "ErrorCode",
    "GetRequest",
    "MemoryResult",
    "Provenance",
    "RecentRequest",
    "RemoveRequest",
    "ReplaceRequest",
    "SearchRequest",
    "StatusRequest",
    "ToolEnvelope",
    "ToolError",
    "ToolErrorEnvelope",
    "ToolMeta",
]
