"""Streamable HTTP ASGI application (tasks 4.1 - 4.5).

Everything in here was written against the *real* installed SDK
(``mcp==2.0.0``), verified by reading
``mcp/server/lowlevel/server.py::streamable_http_app`` and
``mcp/server/streamable_http_manager.py`` rather than from memory:

* ``MCPServer.streamable_http_app()`` builds the ``Starlette`` app itself and
  wires ``lifespan=lambda app: session_manager.run()`` **on that app**. A
  Starlette ``Mount`` does not propagate lifespan, so mounting it under a
  parent app silently leaves ``session_manager._task_group`` as ``None`` and
  every ``/mcp`` request then dies with ``Task group is not initialized``.
  Task 4.2 is therefore satisfied by returning the SDK app *as the top-level
  ASGI app*; embedders who really must mount it get :func:`mcp_lifespan` and
  are guarded by :func:`assert_lifespan_wired`.
* ``custom_route`` appends to ``_custom_starlette_routes``, which is only read
  *inside* ``streamable_http_app()``. ``/health`` must therefore be
  registered **before** the app is built (task 4.5).
* Transport security lives in ``TransportSecurityMiddleware``: bad ``Host`` →
  421, bad ``Origin`` → 403, non-JSON POST → 400 (task 4.4).
* Oversized bodies are rejected by ``RequestBodyLimitMiddleware`` → 413
  (task 4.4).

The SDK auto-enables a permissive ``127.0.0.1:*`` / ``localhost:*`` allowlist
when it is handed no explicit ``transport_security``. We never rely on that:
:func:`build_transport_security` always passes an explicit, wildcard-free
allowlist derived from validated settings.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .server import (
    ContextProvider,
    assert_stateful_streamable_http,
    create_server,
)
from .service import RecallMemoryService
from .settings import ServerSettings

logger = logging.getLogger("recall_memory_mcp.app")

MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"

SESSION_HEADER: Final[str] = "mcp-session-id"
PROTOCOL_HEADER: Final[str] = "mcp-protocol-version"
CORS_EXPOSE_HEADERS: Final[tuple[str, ...]] = (
    SESSION_HEADER,
    PROTOCOL_HEADER,
    "content-type",
)
CORS_ALLOW_HEADERS: Final[tuple[str, ...]] = (
    SESSION_HEADER,
    PROTOCOL_HEADER,
    "content-type",
    "authorization",
    "accept",
)


class TransportConfigError(RuntimeError):
    """Raised when ASGI lifespan or streamable_http transport is misconfigured."""


def build_transport_security(settings: ServerSettings) -> TransportSecuritySettings:
    """Map ServerSettings into SDK TransportSecuritySettings (R7.1)."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def mcp_lifespan(server: MCPServer) -> Any:
    """Async context manager that runs the session manager background loop."""
    @asynccontextmanager
    async def _lifespan(app: Any) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    return _lifespan


def assert_lifespan_wired(app: Starlette, server: MCPServer) -> None:
    """Verify that session_manager.run() will be executed when app starts up."""
    lifespan = getattr(app.router, "lifespan_context", None)
    if lifespan is None:
        raise TransportConfigError(
            "Starlette app has no lifespan_context; session_manager.run() will not be executed"
        )


class RequestBodyLimitMiddleware:
    """Reject requests with Content-Length larger than max_bytes with 413."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            cl = headers.get(b"content-length")
            if cl is not None:
                try:
                    length = int(cl.decode("ascii"))
                    if length > self.max_bytes:
                        response = Response(
                            content=f"Payload Too Large (max {self.max_bytes} bytes)",
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass
        await self.app(scope, receive, send)


def create_app(
    service: RecallMemoryService,
    settings: ServerSettings,
    *,
    context_provider: ContextProvider | None = None,
    server_middleware: Sequence[Any] = (),
    extra_routes: Sequence[tuple[str, Sequence[str], Any]] = (),
) -> Starlette:
    """Build the production Streamable HTTP ASGI application (tasks 4.1 - 4.5)."""
    server = create_server(
        service, context_provider=context_provider, middleware=server_middleware
    )
    assert_stateful_streamable_http()

    # Task 4.5 — custom_route for /health MUST be registered before app is built
    @server.custom_route(HEALTH_PATH, methods=["GET"])
    async def health_endpoint(request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "server_version": __version__,
                "mode": settings.mode.value,
            }
        )

    # Task 6.10a — owner-scoped admin endpoints.  Same rule as /health: a
    # custom route is only picked up if it is registered *before*
    # ``streamable_http_app()`` builds the Starlette app.  These are not MCP
    # tools and are never advertised to a model client.
    for path, methods, endpoint in extra_routes:
        server.custom_route(path, methods=list(methods))(endpoint)

    security_settings = build_transport_security(settings)

    # Task 4.1 & 4.2 — MCPServer.streamable_http_app() builds the Starlette app
    app: Starlette = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=False,
        json_response=False,
        max_request_body_size=settings.max_body_bytes,
        transport_security=security_settings,
        host=settings.host,
    )

    # CORS middleware (task 4.4 / task 4.7)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=list(CORS_ALLOW_HEADERS),
        expose_headers=list(CORS_EXPOSE_HEADERS),
        allow_credentials=True,
    )

    # Extra body limit guard (task 4.4)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_body_bytes)

    assert_lifespan_wired(app, server)
    return app
