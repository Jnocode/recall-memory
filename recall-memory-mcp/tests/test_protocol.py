"""Protocol tests for Streamable HTTP application (task 4.6)."""

from __future__ import annotations

import json
import pytest
from starlette.testclient import TestClient

from recall_memory_mcp.app import (
    HEALTH_PATH,
    MCP_PATH,
    TransportConfigError,
    assert_lifespan_wired,
    create_app,
)
from recall_memory_mcp.server import static_context_provider
from recall_memory_mcp.settings import ServerSettings
from _support import FakeRepository, make_context, make_service

ENV = {
    "RECALL_MCP_HOST": "127.0.0.1",
    "RECALL_MCP_PORT": "19876",
    "RECALL_MCP_MODE": "local",
    "APPDATA": r"C:\Users\test\AppData\Roaming",
}

# Finding H-1 remediation: /health is now behind the same Host allowlist as
# /mcp, so these probes must present a Host the settings actually allow.
# ``TestClient`` otherwise defaults to the bogus ``testserver`` host.
ALLOWED_HOST = {"Host": "127.0.0.1:19876"}


def _make_settings(**overrides: str) -> ServerSettings:
    env = {**ENV, **overrides}
    return ServerSettings.from_env(env)


def _make_app(
    *,
    repository: FakeRepository | None = None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[TestClient, FakeRepository]:
    service, repo = make_service(repository)
    settings = _make_settings(**(env_overrides or {}))
    ctx = make_context()
    app = create_app(
        service,
        settings,
        context_provider=static_context_provider(ctx),
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, repo


class TestLifespan:
    def test_mcp_and_health_routes_present(self) -> None:
        client, _ = _make_app()
        # Health check
        res = client.get(HEALTH_PATH, headers=ALLOWED_HOST)
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    def test_create_app_returns_starlette(self) -> None:
        client, _ = _make_app()
        assert client is not None

    def test_assert_lifespan_wired_passes_on_valid_app(self) -> None:
        client, _ = _make_app()
        assert client.app is not None


class TestProtocol:
    def test_post_accepted(self) -> None:
        client, _ = _make_app()
        init_body = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "test-protocol", "version": "0.1.0"},
            },
        }
        with client:
            resp = client.post(
                MCP_PATH,
                json=init_body,
                headers={"Content-Type": "application/json", "Host": "127.0.0.1:19876"},
            )
            assert resp.status_code == 200, f"initialize failed: {resp.status_code} {resp.text}"

    def test_health_only_returns_permitted_fields(self) -> None:
        client, _ = _make_app()
        resp = client.get(HEALTH_PATH, headers=ALLOWED_HOST)
        data = resp.json()
        assert set(data.keys()) <= {"status", "server_version", "mode"}
        assert "db_path" not in data
        assert "count" not in data
