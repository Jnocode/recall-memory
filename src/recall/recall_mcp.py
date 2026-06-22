#!/usr/bin/env python3
"""recall. MCP Server for Gemini CLI

Provides memory retrieval/storage as MCP tools.
Gemini CLI usage: gemini mcp add recall "python" "path/to/recall_mcp.py"
"""

import sys, json, os, sqlite3

# Add recall to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from embed import embed

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall_p0.db")


def get_store():
    """Lazy-init store for each request."""
    from store import SQLiteStore
    store = SQLiteStore(DB_PATH, vec_dim=768)
    return store


# ─── MCP tool implementations ─────────────────────────────────────────────────

def tool_recall(query: str, k: int = 5) -> dict:
    """Retrieve relevant memories for a query."""
    from retrieve import retrieve_relevant
    store = get_store()
    mems = retrieve_relevant(query, store, k=k)
    results = []
    for m in mems:
        results.append({
            "id": m.id,
            "content": m.content,
            "session_id": m.session_id,
            "timestamp": m.timestamp.isoformat(),
            "tag": m.tag,
        })
    return {"memories": results, "count": len(results)}


def tool_store(content: str, session_id: str = "", tag: str = "episodic") -> dict:
    """Store a memory with auto-keyword indexing."""
    from store import Memory
    from datetime import datetime
    store = get_store()
    mem = Memory(content=content, session_id=session_id, tag=tag,
                 timestamp=datetime.utcnow(), embedding=embed(content))
    mid = store.add(mem)
    return {"id": mid, "status": "stored"}


def tool_stats() -> dict:
    """Get memory store statistics."""
    store = get_store()
    conn = sqlite3.connect(DB_PATH)
    kw_count = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
    conn.close()
    return {
        "memories": store.count(),
        "keywords": kw_count,
    }


TOOLS = {
    "recall": {
        "description": "Retrieve relevant past memories for context",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to remember about"},
                "k": {"type": "integer", "description": "Number of memories to return", "default": 5}
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
        "description": "Get memory store statistics",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "handler": tool_stats,
    },
}


# ─── MCP Stdio Transport ──────────────────────────────────────────────────────

def handle_request(request: dict) -> dict:
    """Process a single MCP JSON-RPC request."""
    req_id = request.get("id")
    method = request.get("method", "")

    # Initialize
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {
                        "listChanged": False
                    }
                },
                "serverInfo": {
                    "name": "recall-mcp",
                    "version": "1.0.0"
                }
            }
        }

    # List tools
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": name,
                        "description": info["description"],
                        "inputSchema": info["input_schema"]
                    }
                    for name, info in TOOLS.items()
                ]
            }
        }

    # Call tool
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

    # Notifications (no response needed)
    if method.startswith("notifications/"):
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"}
    }


if __name__ == "__main__":
    # Read single request from stdin, process, output response
    # Gemini CLI uses stdio transport
    import select
    # Check if we're in stdio mode (Gemini MCP launches us)
    if sys.stdin.isatty():
        print("recall. MCP Server")
        print("Use: gemini mcp add recall python \"{}\"".format(__file__))
        sys.exit(0)

    # MCP stdio mode
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
