#!/usr/bin/env python3
"""recall. MCP Server

Provides memory retrieval/storage as MCP tools via stdio transport.
Usage: python -m recall.recall_mcp
   or: gemini mcp add recall "python" "path/to/src/recall/recall_mcp.py"
"""

import sys, json, os, sqlite3
from datetime import datetime, timezone

from .embed import embed
from .store import SQLiteStore, Memory, extract_keywords
from .retrieve import retrieve_tiered, retrieve_relevant
from .config import DEFAULT_DB_PATH


def get_store():
    return SQLiteStore(DEFAULT_DB_PATH, vec_dim=768)


# ─── MCP tool implementations ─────────────────────────────────────────────────

def tool_recall(query: str, k: int = 5, include_cold: bool = False) -> dict:
    """Retrieve relevant memories using tiered storage."""
    store = get_store()

    if include_cold:
        # Full search across all tiers, including cold for explicit queries
        from .retrieve import retrieve_relevant
        mems = retrieve_relevant(query, store, k=k, tier=None, include_cold=True)
    else:
        # Normal tiered retrieval (hot + warm fill-gap + cold fill-gap)
        mems = retrieve_tiered(query, store, k=k)

    results = []
    for m in mems:
        results.append({
            "id": m.id,
            "content": m.content,
            "session_id": m.session_id,
            "timestamp": m.timestamp.isoformat(),
            "tag": m.tag,
            "tier": m.tier,
            "access_count": m.access_count,
        })
    return {"memories": results, "count": len(results)}


def tool_store(content: str, session_id: str = "", tag: str = "episodic") -> dict:
    """Store a memory with auto-keyword indexing."""
    store = get_store()
    mem = Memory(content=content, session_id=session_id, tag=tag,
                 timestamp=datetime.now(timezone.utc), embedding=embed(content))
    mid = store.add(mem)
    return {"id": mid, "status": "stored"}


def tool_stats() -> dict:
    """Get memory store statistics with tier distribution."""
    store = get_store()
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    kw_count = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
    conn.close()
    tiers = store.get_tier_summary()
    return {
        "memories": store.count(),
        "keywords": kw_count,
        "tiers": tiers,
    }


def tool_gc(dry_run: bool = False) -> dict:
    """Run garbage collection to evict low-score memories."""
    store = get_store()
    result = store.evict(dry_run=dry_run)
    return result


TOOLS = {
    "recall": {
        "description": "Retrieve relevant past memories using tiered storage (hot/warm/cold)",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to remember about"},
                "k": {"type": "integer", "description": "Number of memories to return", "default": 5},
                "include_cold": {"type": "boolean", "description": "Include cold tier in search", "default": False},
            },
            "required": ["query"]
        },
        "handler": tool_recall,
    },
    "store_memory": {
        "description": "Store a new memory for future recall",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory content"},
                "session_id": {"type": "string", "description": "Optional session identifier", "default": ""},
                "tag": {"type": "string", "description": "Memory type (episodic/semantic/procedural)", "default": "episodic"}
            },
            "required": ["content"]
        },
        "handler": tool_store,
    },
    "memory_stats": {
        "description": "Get memory store statistics with tier distribution",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "handler": tool_stats,
    },
    "gc_memory": {
        "description": "Run garbage collection to evict low-score memories",
        "input_schema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Preview without deleting", "default": False}
            },
            "required": []
        },
        "handler": tool_gc,
    },
}


# ─── MCP Stdio Transport ──────────────────────────────────────────────────────

def handle_request(request: dict) -> dict | None:
    req_id = request.get("id")
    method = request.get("method", "")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "recall-mcp", "version": "1.1.0"}
            }
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": name, "description": info["description"],
                     "inputSchema": info["input_schema"]}
                    for name, info in TOOLS.items()
                ]
            }
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name in TOOLS:
            try:
                result = TOOLS[tool_name]["handler"](**arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(e)}
                }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
        }

    if method.startswith("notifications/"):
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"}
    }


if __name__ == "__main__":
    if sys.stdin.isatty():
        print("recall. MCP Server v1.1.0")
        print("Run via: python -m recall.recall_mcp")
        sys.exit(0)

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            continue
        except EOFError:
            break
