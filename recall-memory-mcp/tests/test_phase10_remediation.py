"""Phase 10 task 10.6 grader-finding remediation regressions.

Three independent findings from the fresh-context Maker != Grader review are
locked down here.  Each test was written *before* the fix and observed to fail
against commit ``a30e392``.

H-1 (HIGH, security review): custom Starlette routes registered through
``server.custom_route()`` -- ``/health`` and every ``/admin/v1/*`` endpoint --
never pass through the SDK ``TransportSecurityMiddleware``, which only guards
the ``/mcp`` streamable-HTTP handler.  A DNS-rebound browser therefore reached
the admin surface with ``Host: evil.example.com``.

H-2 (HIGH, security review): ``redact_text`` rule 7 anchors the secret keyword
with ``\\b``, so environment-variable style assignments whose *suffix* is the
keyword (``RECALL_MCP_TOKEN_SECRET=...``, ``RECALL_MCP_DIGEST_KEY=...``)
matched nothing and survived redaction.

P0-1 (protocol review) is *not* reproduced as a product defect: reading the
installed ``mcp==2.0.0`` source shows ``2026-07-28`` is by design a
single-exchange revision (``mcp/server/_streamable_http_modern.py`` docstring:
"no ``initialize`` handshake, no ``Mcp-Session-Id``, one JSON-RPC request in,
one JSON-RPC response out") and its handler answers non-POST with ``405 +
Allow: POST`` on purpose.  The tests below pin that documented behaviour --
including the fact that transport security *is* enforced on that path -- so a
future SDK bump that silently changes it fails loudly.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from recall_memory_mcp.app import HEALTH_PATH, MCP_PATH, create_app
from recall_memory_mcp.redaction import REDACTED, redact_text
from recall_memory_mcp.settings import ServerSettings
from _support import make_context, make_service

ENV = {
    "RECALL_MCP_HOST": "127.0.0.1",
    "RECALL_MCP_PORT": "19876",
    "RECALL_MCP_MODE": "local",
    "APPDATA": r"C:\Users\test\AppData\Roaming",
}

GOOD_HOST = "127.0.0.1:19876"
BAD_HOST = "evil.example.com"
BAD_ORIGIN = "http://evil.example.com"

PROBE_PATH = "/admin/v1/probe"


async def _probe_endpoint(request: Any) -> Any:
    """Stand-in for a real admin endpoint: must never be reached off-host."""
    return JSONResponse({"reached": True}, status_code=200)


def _make_client(**overrides: str) -> TestClient:
    service, _repo = make_service(None)
    settings = ServerSettings.from_env({**ENV, **overrides})
    from recall_memory_mcp.server import static_context_provider

    app = create_app(
        service,
        settings,
        context_provider=static_context_provider(make_context()),
        extra_routes=((PROBE_PATH, ("GET", "POST"), _probe_endpoint),),
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# H-1 -- custom routes must share the /mcp transport security guard
# ---------------------------------------------------------------------------


class TestCustomRouteTransportSecurity:
    def test_health_good_host_still_works(self) -> None:
        client = _make_client()
        with client:
            resp = client.get(HEALTH_PATH, headers={"Host": GOOD_HOST})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"

    def test_health_rejects_rebound_host(self) -> None:
        client = _make_client()
        with client:
            resp = client.get(HEALTH_PATH, headers={"Host": BAD_HOST})
        assert resp.status_code == 421, f"/health served a rebound Host: {resp.status_code}"

    def test_health_rejects_foreign_origin(self) -> None:
        client = _make_client()
        with client:
            resp = client.get(
                HEALTH_PATH, headers={"Host": GOOD_HOST, "Origin": BAD_ORIGIN}
            )
        assert resp.status_code == 403, f"/health served a foreign Origin: {resp.status_code}"

    def test_admin_route_rejects_rebound_host(self) -> None:
        client = _make_client()
        with client:
            resp = client.post(
                PROBE_PATH,
                json={"scope": "project:x"},
                headers={"Host": BAD_HOST, "Content-Type": "application/json"},
            )
        assert resp.status_code == 421, f"admin route served a rebound Host: {resp.status_code}"
        assert "reached" not in resp.text

    def test_admin_route_rejects_foreign_origin(self) -> None:
        client = _make_client()
        with client:
            resp = client.post(
                PROBE_PATH,
                json={"scope": "project:x"},
                headers={
                    "Host": GOOD_HOST,
                    "Origin": BAD_ORIGIN,
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 403, f"admin route served a foreign Origin: {resp.status_code}"
        assert "reached" not in resp.text

    def test_admin_route_good_host_reaches_endpoint(self) -> None:
        client = _make_client()
        with client:
            resp = client.post(
                PROBE_PATH,
                json={"scope": "project:x"},
                headers={"Host": GOOD_HOST, "Content-Type": "application/json"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"reached": True}

    def test_guard_response_leaks_nothing(self) -> None:
        """The rejection body must not echo the attacker host or any path."""
        client = _make_client()
        with client:
            resp = client.get(HEALTH_PATH, headers={"Host": BAD_HOST})
        body = resp.text
        assert BAD_HOST not in body
        assert "C:\\" not in body and "/Users/" not in body


# ---------------------------------------------------------------------------
# H-2 -- suffix-keyed secret assignments must be redacted
# ---------------------------------------------------------------------------


class TestSuffixKeyedSecretRedaction:
    SECRETS = (
        ("RECALL_MCP_TOKEN_SECRET=s3cr3t-value-abc", "s3cr3t-value-abc"),
        ("RECALL_MCP_DIGEST_KEY=0123456789abcdef0123", "0123456789abcdef0123"),
        ("MYAPP_DB_PASSWORD: hunter2hunter2", "hunter2hunter2"),
        ("OAUTH_CLIENT_SECRET = 'quoted-secret-1234'", "quoted-secret-1234"),
        ("SERVICE_ACCOUNT_PRIVATE_KEY=MIIEvgIBADANBgkq", "MIIEvgIBADANBgkq"),
        ("X_API_TOKEN=abcdefghijklmnop", "abcdefghijklmnop"),
    )

    def test_suffix_keyed_assignments_are_redacted(self) -> None:
        for line, secret in self.SECRETS:
            out = redact_text(line)
            assert secret not in out, f"secret survived redaction: {line!r} -> {out!r}"
            assert REDACTED in out, f"no redaction marker for {line!r} -> {out!r}"

    def test_keyword_prefix_is_preserved_for_operators(self) -> None:
        out = redact_text("RECALL_MCP_TOKEN_SECRET=s3cr3t-value-abc")
        assert out.startswith("RECALL_MCP_TOKEN_SECRET")

    def test_subprocess_stderr_shape_is_redacted(self) -> None:
        stderr = (
            "env: RECALL_MCP_TOKEN_SECRET=abcd1234efgh5678 "
            "RECALL_MCP_DIGEST_KEY=deadbeefdeadbeef\n"
            "error: service failed to start\n"
        )
        out = redact_text(stderr)
        assert "abcd1234efgh5678" not in out
        assert "deadbeefdeadbeef" not in out
        assert "service failed to start" in out

    def test_non_secret_uppercase_assignments_survive(self) -> None:
        """No over-redaction: ordinary config must stay readable."""
        for benign in (
            "RECALL_MCP_PORT=19876",
            "RECALL_MCP_MODE=local",
            "LOG_LEVEL=INFO",
            "MAX_BODY_BYTES=1048576",
        ):
            assert redact_text(benign) == benign, benign

    def test_monkeyed_word_still_matches_plain_keyword(self) -> None:
        """The original rule-7 behaviour must not regress."""
        assert "abc123def456" not in redact_text("token=abc123def456")
        assert "abc123def456" not in redact_text('"client_secret": "abc123def456"')


# ---------------------------------------------------------------------------
# P0-1 -- 2026-07-28 single-exchange semantics are SDK-intentional
# ---------------------------------------------------------------------------


class TestModernProtocolVersionContract:
    HEADERS = {
        "Host": GOOD_HOST,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
    }

    def test_sdk_documents_single_exchange_semantics(self) -> None:
        """Grounding: assert on the installed SDK, not on memory."""
        import mcp.server._streamable_http_modern as modern

        doc = " ".join((modern.__doc__ or "").split())
        assert "2026-07-28" in doc
        assert "no `initialize` handshake" in doc
        assert "no `Mcp-Session-Id`" in doc
        assert "one JSON-RPC request in, one JSON-RPC response out" in doc

    def test_modern_post_is_answered_not_405(self) -> None:
        client = _make_client()
        with client:
            resp = client.post(
                MCP_PATH,
                content=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ).encode(),
                headers=self.HEADERS,
            )
        assert resp.status_code != 405, "modern POST must not be method-rejected"
        assert resp.status_code != 421, resp.text

    def test_modern_get_is_405_with_allow_post(self) -> None:
        client = _make_client()
        with client:
            resp = client.get(MCP_PATH, headers=self.HEADERS)
        assert resp.status_code == 405
        assert resp.headers.get("allow") == "POST"

    def test_modern_delete_is_405_with_allow_post(self) -> None:
        client = _make_client()
        with client:
            resp = client.delete(MCP_PATH, headers=self.HEADERS)
        assert resp.status_code == 405
        assert resp.headers.get("allow") == "POST"

    def test_modern_path_still_enforces_transport_security(self) -> None:
        client = _make_client()
        with client:
            resp = client.post(
                MCP_PATH,
                content=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ).encode(),
                headers={**self.HEADERS, "Host": BAD_HOST},
            )
        assert resp.status_code == 421, resp.text

    def test_legacy_handshake_version_still_stateful(self) -> None:
        """Task 3.12's stateful guarantee is unchanged for handshake revisions."""
        client = _make_client()
        with client:
            resp = client.post(
                MCP_PATH,
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "0"},
                        },
                    }
                ).encode(),
                headers={
                    "Host": GOOD_HOST,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("mcp-session-id"), "stateful session id missing"
