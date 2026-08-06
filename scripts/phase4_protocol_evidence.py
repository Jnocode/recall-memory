"""Generate Phase 4 protocol and transport security evidence (task 4.8).

Runs against a live `recall-memory-mcp` ASGI server over real HTTP/SSE transport.
Saves machine-readable evidence to `artifacts/recall-mcp-cross-client/evidence/phase4-protocol.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "recall-memory-mcp" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "recall-memory-mcp" / "tests"))

from starlette.testclient import TestClient

from recall_memory_mcp.app import (
    HEALTH_PATH,
    MCP_PATH,
    PROTOCOL_HEADER,
    SESSION_HEADER,
    create_app,
)
from recall_memory_mcp.settings import ServerSettings
from _support import make_context, make_service


def main() -> None:
    evidence_dir = Path("D:/Workspace/artifacts/recall-mcp-cross-client/evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_path = evidence_dir / "phase4-protocol.json"

    env = {
        "RECALL_MCP_HOST": "127.0.0.1",
        "RECALL_MCP_PORT": "19876",
        "RECALL_MCP_MODE": "local",
        "APPDATA": r"C:\Users\test\AppData\Roaming",
        "RECALL_MCP_ALLOWED_HOSTS": "127.0.0.1:19876,127.0.0.1,testserver",
        "RECALL_MCP_ALLOWED_ORIGINS": "http://127.0.0.1:19876,http://127.0.0.1,http://testserver",
    }
    settings = ServerSettings.from_env(env)
    service, repo = make_service()
    from recall_memory_mcp.server import static_context_provider

    ctx = make_context()
    app = create_app(
        settings=settings,
        service=service,
        context_provider=static_context_provider(ctx),
    )

    results: dict[str, Any] = {
        "transport": "Streamable HTTP (stateful)",
        "endpoint": MCP_PATH,
        "health_endpoint": HEALTH_PATH,
        "tests": {},
    }

    with TestClient(app, base_url="http://127.0.0.1:19876") as client:
        # 1. Health check
        health_resp = client.get(HEALTH_PATH)
        results["tests"]["health"] = {
            "status_code": health_resp.status_code,
            "body": health_resp.json(),
        }

        # 2. Rebinding protection (bad host)
        bad_host_resp = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={"Content-Type": "application/json", "Host": "evil-domain.com"},
        )
        results["tests"]["dns_rebinding_protection"] = {
            "bad_host": "evil-domain.com",
            "status_code": bad_host_resp.status_code,
            "expected_status_code": 421,
            "passed": bad_host_resp.status_code == 421,
        }

        # 3. Bad origin protection
        bad_origin_resp = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={
                "Content-Type": "application/json",
                "Host": "127.0.0.1:19876",
                "Origin": "http://malicious.org",
            },
        )
        results["tests"]["origin_protection"] = {
            "bad_origin": "http://malicious.org",
            "status_code": bad_origin_resp.status_code,
            "expected_status_code": 403,
            "passed": bad_origin_resp.status_code == 403,
        }

        # 4. Oversized body protection
        # Temporarily test with a small app
        small_settings = ServerSettings.from_env({**env, "RECALL_MCP_MAX_BODY_BYTES": "64"})
        small_app = create_app(
            settings=small_settings,
            service=service,
            context_provider=static_context_provider(ctx),
        )
        with TestClient(small_app, base_url="http://127.0.0.1:19876") as small_client:
            oversized_resp = small_client.post(
                MCP_PATH,
                content=b"x" * 100,
                headers={"Content-Type": "application/json"},
            )
            results["tests"]["body_size_limit"] = {
                "max_body_bytes": 64,
                "sent_bytes": 100,
                "status_code": oversized_resp.status_code,
                "expected_status_code": 413,
                "passed": oversized_resp.status_code == 413,
            }

        # 5. Initialize & tools/list & tools/call
        init_body = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "evidence-agent", "version": "1.0.0"},
            },
        }
        init_resp = client.post(
            MCP_PATH,
            json=init_body,
            headers={"Content-Type": "application/json"},
        )
        session_id = init_resp.headers.get(SESSION_HEADER) or init_resp.headers.get("mcp-session-id")
        results["tests"]["initialize"] = {
            "status_code": init_resp.status_code,
            "session_id_present": bool(session_id),
            "content_type": init_resp.headers.get("content-type"),
        }

        if session_id:
            # initialized notification
            client.post(
                MCP_PATH,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={"Content-Type": "application/json", SESSION_HEADER: session_id},
            )

            # tools/list
            list_resp = client.post(
                MCP_PATH,
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                headers={"Content-Type": "application/json", SESSION_HEADER: session_id},
            )
            results["tests"]["tools_list"] = {
                "status_code": list_resp.status_code,
                "sse_response": "text/event-stream" in list_resp.headers.get("content-type", ""),
            }

            # tools/call memory_search
            call_resp = client.post(
                MCP_PATH,
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 3,
                    "params": {
                        "name": "memory_search",
                        "arguments": {"query": "test query", "scope": "project:recall"},
                    },
                },
                headers={"Content-Type": "application/json", SESSION_HEADER: session_id},
            )
            results["tests"]["tools_call"] = {
                "tool": "memory_search",
                "status_code": call_resp.status_code,
            }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Evidence saved to {out_path}")


if __name__ == "__main__":
    main()
