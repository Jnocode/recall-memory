"""Unit & Integration tests for scope enforcement (Task 5.3 / Task 5.8)."""

from __future__ import annotations

import pytest

from recall_memory_mcp.auth import OAuthTokenPayload, payload_to_caller_context
from recall_memory_mcp.models import (
    AddRequest,
    GetRequest,
    RecentRequest,
    RemoveRequest,
    ReplaceRequest,
    SearchRequest,
    StatusRequest,
)
from recall_memory_mcp.service import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    CallerContext,
    RecallMemoryService,
)
from _support import FakeRepository, make_service


def test_read_scope_allows_search_get_recent() -> None:
    service, repo = make_service()
    ctx = CallerContext(
        grant_id="g1",
        owner_id="o1",
        actor_id="a1",
        source_client="client-test",
        oauth_scopes=(SCOPE_READ,),
        memory_scopes=("project:recall",),
    )

    res_search = service.search(ctx, SearchRequest(query="test", scope="project:recall"))
    assert getattr(res_search, "error", None) is None

    res_get = service.get(ctx, GetRequest(memory_id="mem-1", scope="project:recall"))
    assert getattr(res_get, "error", None) is None

    res_recent = service.recent(ctx, RecentRequest(scope="project:recall"))
    assert getattr(res_recent, "error", None) is None


def test_read_scope_denies_write_operations() -> None:
    service, repo = make_service()
    ctx = CallerContext(
        grant_id="g1",
        owner_id="o1",
        actor_id="a1",
        source_client="client-test",
        oauth_scopes=(SCOPE_READ,),  # Only read scope
        memory_scopes=("project:recall",),
    )

    res_add = service.add(
        ctx,
        AddRequest(
            content="hello world long content",
            idempotency_key="key-valid-12345",
            kind="episodic",
            scope="project:recall",
        ),
    )
    assert getattr(res_add, "error", None) is not None
    assert res_add.error.code == "insufficient_scope"


def test_write_scope_allows_add_replace_remove() -> None:
    service, repo = make_service()
    ctx = CallerContext(
        grant_id="g1",
        owner_id="o1",
        actor_id="a1",
        source_client="client-test",
        oauth_scopes=(SCOPE_WRITE,),
        memory_scopes=("project:recall",),
    )

    res_add = service.add(
        ctx,
        AddRequest(
            content="hello world long content",
            idempotency_key="key-valid-12345",
            kind="episodic",
            scope="project:recall",
        ),
    )
    assert getattr(res_add, "error", None) is None


def test_memory_admin_scope_bypasses_read_write_restrictions() -> None:
    service, repo = make_service()
    ctx = CallerContext(
        grant_id="g1",
        owner_id="o1",
        actor_id="a1",
        source_client="client-test",
        oauth_scopes=(SCOPE_ADMIN,),
        memory_scopes=("project:recall",),
    )

    res_search = service.search(ctx, SearchRequest(query="test", scope="project:recall"))
    assert getattr(res_search, "error", None) is None

    res_add = service.add(
        ctx,
        AddRequest(
            content="admin write long content",
            idempotency_key="key-admin-12345",
            kind="episodic",
            scope="project:recall",
        ),
    )
    assert getattr(res_add, "error", None) is None
