"""Transport security tests (task 4.7)."""

from __future__ import annotations

import json
import pytest
from starlette.testclient import TestClient

from recall_memory_mcp.app import (
    CORS_ALLOW_HEADERS,
    CORS_EXPOSE_HEADERS,
    MCP_PATH,
    PROTOCOL_HEADER,
    SESSION_HEADER,
    build_transport_security,
    create_app,
)
from recall_memory_mcp.settings import ServerSettings, SettingsError
from _support import FakeRepository, make_context, make_service

ENV = {
    "RECALL_MCP_HOST": "127.0.0.1",
    "RECALL_MCP_PORT": "19876",
    "RECALL_MCP_MODE": "local",
    "APPDATA": r"C:\Users\test\AppData\Roaming",
}


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
    from recall_memory_mcp.server import static_context_provider

    ctx = make_context()
    app = create_app(
        service,
        settings,
        context_provider=static_context_provider(ctx),
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, repo


class TestDNSRebindingProtection:
    def test_good_host_accepted(self) -> None:
        client, _ = _make_app()
        with client:
            resp = client.post(
                MCP_PATH,
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
                headers={
                    "Content-Type": "application/json",
                    "Host": "127.0.0.1:19876",
                },
            )
            assert resp.status_code != 421, f"good host rejected: {resp.status_code} {resp.text}"

    def test_bad_host_rejected_with_421(self) -> None:
        client, _ = _make_app()
        with client:
            resp = client.post(
                MCP_PATH,
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
                headers={
                    "Content-Type": "application/json",
                    "Host": "attacker.com",
                },
            )
            assert resp.status_code == 421, f"bad host was not 421: {resp.status_code} {resp.text}"

    def test_bad_origin_rejected_with_403(self) -> None:
        client, _ = _make_app()
        with client:
            resp = client.post(
                MCP_PATH,
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
                headers={
                    "Content-Type": "application/json",
                    "Host": "127.0.0.1:19876",
                    "Origin": "http://evil-site.com",
                },
            )
            assert resp.status_code == 403, f"bad origin was not 403: {resp.status_code} {resp.text}"


class TestOversizedBodyLimit:
    def test_oversized_body_returns_413(self) -> None:
        client, _ = _make_app(env_overrides={"RECALL_MCP_MAX_BODY_BYTES": "500"})
        big_content = "A" * 1000
        with client:
            resp = client.post(
                MCP_PATH,
                content=json.dumps({"jsonrpc": "2.0", "method": "test", "params": {"data": big_content}}).encode(),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 413, f"oversized body was not 413: {resp.status_code} {resp.text}"


class TestWildcardProtection:
    def test_build_transport_security_rejects_wildcard(self) -> None:
        settings = _make_settings()
        ts = build_transport_security(settings)
        assert ts.enable_dns_rebinding_protection is True
        assert len(ts.allowed_hosts) > 0
        assert len(ts.allowed_origins) > 0

    def test_settings_from_env_rejects_wildcard_hosts(self) -> None:
        with pytest.raises(SettingsError):
            _make_settings(RECALL_MCP_ALLOWED_HOSTS="*")

    def test_settings_from_env_rejects_wildcard_origins(self) -> None:
        with pytest.raises(SettingsError):
            _make_settings(RECALL_MCP_ALLOWED_ORIGINS="*")
