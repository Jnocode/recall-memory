"""Online admin API + OS-bound local admin session (task 6.10a).

The endpoints are exercised through the **real ASGI application** built by
``authority.build_app`` — same routing, same middleware stack, same request
parsing a browser or the CLI would hit.  ``httpx`` is not installed in the
distribution runtime, so instead of pulling in a test-only HTTP client this
module speaks ASGI directly: a synthetic ``http`` scope, a real ``receive``
and a real ``send``.  That is strictly closer to the wire than a mocked
endpoint function would be.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from recall_memory_mcp import admin, adminapi, authority, models, provisioning
from recall_memory_mcp.settings import ServerSettings

pytest.importorskip("recall.mcp_repository", reason="the canonical recall-sqlite core is required")

from test_admin import (  # noqa: E402 - shared real-authority helpers
    CANARY,
    OTHER_SCOPE,
    SCOPE,
    StubEmbedder,
    data_of,
)

PORT = 19883


def make_settings(tmp_path: Path, **overrides: str) -> ServerSettings:
    env = {
        "RECALL_MCP_CONFIG_DIR": str(tmp_path / "authority"),
        "RECALL_MCP_MODE": "local",
        "RECALL_MCP_HOST": "127.0.0.1",
        "RECALL_MCP_PORT": str(PORT),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"),
        "TEMP": str(tmp_path / "temp"),
    }
    env.update(overrides)
    return ServerSettings.from_env(env)


def build_stack(tmp_path: Path, *, memory_scopes: tuple[str, ...] = (SCOPE,)) -> tuple[Any, ...]:
    settings = make_settings(tmp_path)
    provisioning.initialize(settings)
    service = authority.build_service(
        settings,
        embedder=StubEmbedder(),
        env={},
        digest_key=b"deterministic-test-digest-key",
        clock=lambda: "2026-08-05T00:00:00+00:00",
    )
    app = authority.build_app(
        settings, service=service, env={}, memory_scopes=memory_scopes
    )
    session = adminapi.load_local_admin_session(settings.config_dir)
    grant = authority.ensure_local_grant(settings.db_path, memory_scopes=memory_scopes)
    return settings, service, app, session, grant


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


def asgi_post(
    app: Any,
    path: str,
    payload: Any,
    *,
    token: str | None,
    client: tuple[str, int] = ("127.0.0.1", 54321),
    content_type: str = "application/json",
    host: str = f"127.0.0.1:{PORT}",
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
    headers = [
        (b"host", host.encode()),
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
        (b"accept", b"application/json"),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": client,
        "server": ("127.0.0.1", PORT),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        decoded = {"raw": raw.decode("utf-8", "replace")}
    return status, decoded


def seed(service: Any, grant: Any, *, content: str = CANARY, scope: str = SCOPE) -> dict[str, Any]:
    ctx = grant.caller_context()
    return data_of(
        service.add(
            ctx,
            models.AddRequest(
                content=content,
                scope=scope,
                kind="note",
                tags=("canary",),
                idempotency_key="key-seed-0001",
            ),
        )
    )


# ---------------------------------------------------------------------------
# session file
# ---------------------------------------------------------------------------


class TestLocalAdminSession:
    def test_serve_time_session_is_written_owner_only(self, tmp_path: Path) -> None:
        settings, _service, _app, session, _grant = build_stack(tmp_path)
        path = adminapi.session_path_for(settings.config_dir)
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["endpoint"] == f"http://127.0.0.1:{PORT}"
        assert payload["token"] == session.token
        assert len(session.token) >= 32
        if hasattr(os, "getuid"):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o077 == 0, f"session file is group/other readable: {oct(mode)}"

    def test_missing_session_says_there_is_no_offline_path(self, tmp_path: Path) -> None:
        with pytest.raises(adminapi.AdminSessionError) as caught:
            adminapi.load_local_admin_session(tmp_path / "nowhere")
        assert "offline" in str(caught.value)

    def test_session_of_another_os_account_is_refused(self, tmp_path: Path) -> None:
        settings, *_ = build_stack(tmp_path)
        path = adminapi.session_path_for(settings.config_dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["os_account"] = "user:somebody-else"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(adminapi.AdminSessionError) as caught:
            adminapi.load_local_admin_session(settings.config_dir)
        assert "OS account" in str(caught.value)

    def test_expired_session_is_refused(self, tmp_path: Path) -> None:
        settings, *_ = build_stack(tmp_path)
        path = adminapi.session_path_for(settings.config_dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(adminapi.AdminSessionError) as caught:
            adminapi.load_local_admin_session(settings.config_dir)
        assert "expired" in str(caught.value)

    def test_incomplete_session_is_refused(self, tmp_path: Path) -> None:
        settings, *_ = build_stack(tmp_path)
        path = adminapi.session_path_for(settings.config_dir)
        path.write_text(json.dumps({"token": "x"}), encoding="utf-8")
        with pytest.raises(adminapi.AdminSessionError):
            adminapi.load_local_admin_session(settings.config_dir)

    def test_discard_removes_the_session(self, tmp_path: Path) -> None:
        settings, *_ = build_stack(tmp_path)
        assert adminapi.discard_local_admin_session(settings.config_dir) is True
        assert not adminapi.session_path_for(settings.config_dir).exists()
        assert adminapi.discard_local_admin_session(settings.config_dir) is False

    def test_session_repr_never_shows_the_token(self, tmp_path: Path) -> None:
        _settings, _service, _app, session, _grant = build_stack(tmp_path)
        assert session.token not in json.dumps(session.redacted())


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


class TestAdminEndpoints:
    def test_export_with_the_session_token_returns_the_scope(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        row = seed(service, grant)
        status, payload = asgi_post(
            app, adminapi.EXPORT_PATH, {"scope": SCOPE}, token=session.token
        )
        assert status == 200, payload
        assert payload["auth"] == "local_admin_session"
        assert payload["memory_count"] == 1
        assert payload["memories"][0]["memory_id"] == row["memory_id"]
        assert payload["memories"][0]["content"] == CANARY
        assert payload["includes_tombstones"] is False
        assert "backup_retention_notice" in payload

    def test_missing_token_is_401(self, tmp_path: Path) -> None:
        _settings, service, app, _session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, payload = asgi_post(app, adminapi.EXPORT_PATH, {"scope": SCOPE}, token=None)
        assert status == 401
        assert payload == {"error": "unauthorized"}

    def test_wrong_token_is_401(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, _payload = asgi_post(
            app, adminapi.EXPORT_PATH, {"scope": SCOPE}, token=session.token + "x"
        )
        assert status == 401

    def test_non_loopback_peer_is_refused(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, payload = asgi_post(
            app,
            adminapi.EXPORT_PATH,
            {"scope": SCOPE},
            token=session.token,
            client=("10.1.2.3", 4444),
        )
        assert status == 403
        assert payload == {"error": "not_authorized"}

    def test_unreachable_scope_is_refused(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, payload = asgi_post(
            app, adminapi.EXPORT_PATH, {"scope": OTHER_SCOPE}, token=session.token
        )
        assert status == 403
        assert payload == {"error": "not_authorized"}

    def test_owner_id_in_the_request_is_refused(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, payload = asgi_post(
            app,
            adminapi.EXPORT_PATH,
            {"scope": SCOPE, "owner_id": "owner-somebody-else"},
            token=session.token,
        )
        assert status == 400
        assert payload == {"error": "validation_error"}

    def test_grant_id_in_the_request_is_refused(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, _payload = asgi_post(
            app,
            adminapi.EXPORT_PATH,
            {"scope": SCOPE, "grant_id": "grant-somebody-else"},
            token=session.token,
        )
        assert status == 400

    def test_non_json_content_type_is_refused(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        seed(service, grant)
        status, _payload = asgi_post(
            app,
            adminapi.EXPORT_PATH,
            {"scope": SCOPE},
            token=session.token,
            content_type="text/plain",
        )
        assert status == 400

    def test_purge_then_restore_fails_closed_over_http(self, tmp_path: Path) -> None:
        settings, service, app, session, grant = build_stack(tmp_path)
        row = seed(service, grant)
        status, purged = asgi_post(
            app,
            adminapi.PURGE_PATH,
            {
                "memory_id": row["memory_id"],
                "scope": SCOPE,
                "expected_revision": row["revision"],
                "idempotency_key": "key-http-purge-1",
            },
            token=session.token,
        )
        assert status == 200, purged
        assert purged["final_revision"] == row["revision"] + 1
        assert "backup_retention_notice" in purged

        status, refused = asgi_post(
            app,
            adminapi.RESTORE_PATH,
            {
                "memory_id": row["memory_id"],
                "scope": SCOPE,
                "expected_revision": purged["final_revision"],
                "idempotency_key": "key-http-restore-1",
            },
            token=session.token,
        )
        assert status == 403
        assert refused == {"error": "not_authorized"}

    def test_revision_conflict_reports_the_current_revision(self, tmp_path: Path) -> None:
        _settings, service, app, session, grant = build_stack(tmp_path)
        row = seed(service, grant)
        status, payload = asgi_post(
            app,
            adminapi.PURGE_PATH,
            {
                "memory_id": row["memory_id"],
                "scope": SCOPE,
                "expected_revision": row["revision"] + 5,
                "idempotency_key": "key-http-purge-2",
            },
            token=session.token,
        )
        assert status == 409
        assert payload["current_revision"] == row["revision"]

    def test_admin_endpoints_are_not_mcp_tools(self, tmp_path: Path) -> None:
        """A model client must never see purge/export in the tool list."""

        _settings, service, _app, _session, _grant = build_stack(tmp_path)
        from recall_memory_mcp.server import TOOL_NAMES

        assert not any("purge" in name or "export" in name for name in TOOL_NAMES)

    def test_admin_route_paths_are_registered_exactly_once(self, tmp_path: Path) -> None:
        _settings, _service, app, _session, _grant = build_stack(tmp_path)
        paths = [getattr(route, "path", None) for route in app.router.routes]
        for path in (adminapi.EXPORT_PATH, adminapi.RESTORE_PATH, adminapi.PURGE_PATH):
            assert paths.count(path) == 1, f"{path} registered {paths.count(path)} times"


# ---------------------------------------------------------------------------
# authenticator unit-level checks
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, headers: dict[str, str], client_host: str | None = "127.0.0.1") -> None:
        self.headers = headers
        self.client = type("C", (), {"host": client_host})() if client_host else None


def make_ctx(scopes: tuple[str, ...]) -> Any:
    from recall_memory_mcp.service import CallerContext

    return CallerContext(
        grant_id="grant-remote",
        owner_id="owner-local",
        actor_id="actor:remote",
        source_client="claude",
        oauth_scopes=scopes,
        memory_scopes=(SCOPE,),
    )


class TestAdminAuthenticator:
    def _session(self, tmp_path: Path) -> adminapi.LocalAdminSession:
        return adminapi.create_local_admin_session(
            tmp_path, endpoint=f"http://127.0.0.1:{PORT}"
        )

    def test_oauth_token_without_admin_scope_is_not_an_admin(self, tmp_path: Path) -> None:
        auth = adminapi.AdminAuthenticator(
            session=None,
            local_context_factory=None,
            oauth_resolver=lambda token: make_ctx(("memory:read", "memory:write")),
        )
        ctx, reason = auth.resolve(_FakeRequest({"authorization": "Bearer whatever"}))
        assert ctx is None
        assert reason == "insufficient_grant"

    def test_oauth_token_with_admin_scope_is_accepted(self, tmp_path: Path) -> None:
        auth = adminapi.AdminAuthenticator(
            session=None,
            local_context_factory=None,
            oauth_resolver=lambda token: make_ctx(("memory:admin",)),
        )
        ctx, reason = auth.resolve(_FakeRequest({"authorization": "Bearer whatever"}))
        assert ctx is not None
        assert reason == "oauth_grant"

    def test_revoked_grant_resolves_to_nothing(self, tmp_path: Path) -> None:
        auth = adminapi.AdminAuthenticator(
            session=None, local_context_factory=None, oauth_resolver=lambda token: None
        )
        ctx, reason = auth.resolve(_FakeRequest({"authorization": "Bearer stale"}))
        assert ctx is None
        assert reason == "insufficient_grant"

    def test_expired_session_token_falls_through_to_oauth(self, tmp_path: Path) -> None:
        session = self._session(tmp_path)
        expired = adminapi.LocalAdminSession(
            token=session.token,
            os_account=session.os_account,
            endpoint=session.endpoint,
            created_at=session.created_at,
            expires_at="2000-01-01T00:00:00+00:00",
            path=session.path,
        )
        auth = adminapi.AdminAuthenticator(
            session=expired,
            local_context_factory=lambda: make_ctx(("memory:admin",)),
            oauth_resolver=lambda token: None,
        )
        ctx, reason = auth.resolve(_FakeRequest({"authorization": f"Bearer {session.token}"}))
        assert ctx is None
        assert reason == "insufficient_grant"

    def test_non_bearer_header_is_ignored(self, tmp_path: Path) -> None:
        session = self._session(tmp_path)
        auth = adminapi.AdminAuthenticator(
            session=session, local_context_factory=lambda: make_ctx(("memory:admin",))
        )
        ctx, reason = auth.resolve(_FakeRequest({"authorization": f"Basic {session.token}"}))
        assert ctx is None
        assert reason == "missing_token"

    def test_no_client_information_is_not_loopback(self, tmp_path: Path) -> None:
        session = self._session(tmp_path)
        auth = adminapi.AdminAuthenticator(
            session=session, local_context_factory=lambda: make_ctx(("memory:admin",))
        )
        ctx, reason = auth.resolve(
            _FakeRequest({"authorization": f"Bearer {session.token}"}, client_host=None)
        )
        assert ctx is None
        assert reason == "non_loopback"
