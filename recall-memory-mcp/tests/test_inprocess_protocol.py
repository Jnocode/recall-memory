"""Tasks 3.11 and 3.12 — official-SDK protocol tests without stdio.

3.11: initialize -> tools/list -> tools/call is driven by the real
``mcp.client.Client`` over the SDK's in-memory transport, so no subprocess,
no pipes and no sockets are involved.

3.12: the MVP transport contract is stateful Streamable HTTP.  If anybody
turns on ``stateless_http`` or JSON-only mode, these tests fail.
"""

from __future__ import annotations

import inspect

import pytest
from mcp.server.mcpserver import MCPServer

from recall_memory_mcp import __version__
from recall_memory_mcp.server import (
    SERVER_NAME,
    TOOL_NAMES,
    TransportContractError,
    assert_stateful_streamable_http,
)

from _support import SCOPE, connected, make_server, run


# --------------------------------------------------------------------------
# task 3.11 — in-process protocol round trip
# --------------------------------------------------------------------------


def test_initialize_reports_identity_and_a_current_protocol_version():
    server, _ = make_server()

    async def _go():
        async with connected(server) as client:
            return (
                client.protocol_version,
                client.server_info,
                client.server_capabilities,
                client.instructions,
            )

    protocol_version, server_info, capabilities, instructions = run(_go())
    assert server_info.name == SERVER_NAME
    assert server_info.version == __version__
    # The negotiated version must be a dated MCP revision, not a placeholder.
    assert protocol_version and protocol_version[:4].isdigit()
    assert capabilities.tools is not None
    assert "data_trust" in (instructions or "")


def test_full_round_trip_lists_then_calls_every_read_tool():
    server, repository = make_server()

    async def _go():
        async with connected(server) as client:
            listing = await client.list_tools()
            names = sorted(tool.name for tool in listing.tools)
            search = await client.call_tool(
                "memory_search", {"query": "recall", "scope": SCOPE}
            )
            recent = await client.call_tool("memory_recent", {"scope": SCOPE})
            got = await client.call_tool(
                "memory_get", {"memory_id": "mem-1", "scope": SCOPE}
            )
            status = await client.call_tool("memory_status", {})
            return names, search, recent, got, status

    names, search, recent, got, status = run(_go())
    assert names == sorted(TOOL_NAMES)
    for result in (search, recent, got, status):
        assert result.is_error is False
        assert result.structured_content["ok"] is True
    assert repository.wrote_anything() is False


def test_two_sequential_sessions_share_one_service_instance():
    """A reconnect must not rebuild state or replay side effects."""

    server, repository = make_server()

    async def _go():
        async with connected(server) as client:
            first = await client.call_tool(
                "memory_add",
                {
                    "content": "durable fact",
                    "scope": SCOPE,
                    "kind": "decision",
                    "idempotency_key": "idem-key-session1",
                },
            )
        async with connected(server) as client:
            second = await client.call_tool(
                "memory_get", {"memory_id": "mem-1", "scope": SCOPE}
            )
        return first, second

    first, second = run(_go())
    assert first.structured_content["ok"] is True
    assert second.structured_content["ok"] is True
    assert repository.method_names().count("add_memory") == 1


def test_no_stdio_or_socket_transport_is_used_by_these_tests():
    """Guard against silently regressing 3.11 into a subprocess test."""

    source = inspect.getsource(test_full_round_trip_lists_then_calls_every_read_tool)
    for forbidden in ("stdio", "subprocess", "socket", "http://"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# task 3.12 — stateful Streamable HTTP contract
# --------------------------------------------------------------------------


def test_default_streamable_http_configuration_is_stateful():
    signature = inspect.signature(MCPServer.streamable_http_app)
    assert signature.parameters["stateless_http"].default is False
    assert signature.parameters["json_response"].default is False


def test_contract_accepts_the_stateful_default():
    assert_stateful_streamable_http(stateless_http=False, json_response=False) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stateless_http": True, "json_response": False},
        {"stateless_http": False, "json_response": True},
        {"stateless_http": True, "json_response": True},
    ],
)
def test_contract_rejects_stateless_or_json_only_mode(kwargs):
    with pytest.raises(TransportContractError) as excinfo:
        assert_stateful_streamable_http(**kwargs)
    message = str(excinfo.value)
    assert "stateful" in message
    for flag, enabled in kwargs.items():
        if enabled:
            assert flag in message
