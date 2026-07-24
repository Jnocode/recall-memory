"""Tests for P0 improvements: score propagation, session isolation,
sync daemon, and recall-server MCP endpoint."""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from recall.store import Memory, SQLiteStore
from recall.retrieve import retrieve_relevant


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(str(tmp_path / "test.db"))


def _add(store, content, session_id="", tag="episodic"):
    return store.add(Memory(content=content, session_id=session_id, tag=tag))


# ─── P0-1: score propagation ──────────────────────────────────────────────────

def test_memory_score_default_zero():
    m = Memory(content="hello")
    assert m.score == 0.0


def test_retrieve_fills_score(store):
    _add(store, "User prefers docker-compose for deployment")
    _add(store, "PostgreSQL is the main database")
    results = retrieve_relevant("docker deployment", store, k=5)
    assert results, "should find at least one memory"
    assert all(m.score > 0 for m in results), \
        f"scores: {[m.score for m in results]}"


def test_results_sorted_by_score(store):
    _add(store, "docker-compose preferred for docker deployment workflows")
    _add(store, "The weather in Taipei is humid")
    results = retrieve_relevant("docker deployment", store, k=5)
    scores = [m.score for m in results]
    assert scores == sorted(scores, reverse=True)


# ─── P0-2: session isolation ──────────────────────────────────────────────────

def test_session_filter_isolates(store):
    _add(store, "recall_mobile uses flutter deploy pipeline", session_id="proj-a")
    _add(store, "jojo trader deploy strategy uses binance api", session_id="proj-b")
    results = retrieve_relevant("deploy", store, k=10,
                                session_id_filter="proj-a")
    assert results
    for m in results:
        assert m.session_id in ("proj-a", ""), \
            f"leaked memory from session {m.session_id!r}"


def test_session_filter_keeps_legacy_empty(store):
    _add(store, "legacy deploy knowledge without session", session_id="")
    _add(store, "proj-a docker deploy notes", session_id="proj-a")
    results = retrieve_relevant("deploy", store, k=10,
                                session_id_filter="proj-a")
    contents = " ".join(m.content for m in results)
    assert "legacy" in contents, "legacy empty-session memories must remain visible"


def test_no_filter_returns_all_sessions(store):
    _add(store, "alpha docker deploy", session_id="a")
    _add(store, "beta docker deploy", session_id="b")
    results = retrieve_relevant("docker deploy", store, k=10)
    sessions = {m.session_id for m in results}
    assert sessions >= {"a", "b"}


def test_store_sql_session_filter(store):
    _add(store, "kubernetes cluster setup", session_id="x")
    _add(store, "kubernetes helm charts", session_id="y")
    ids_x = store.search_by_keywords(["kubernetes"], session_id="x")
    for mid in ids_x:
        mem = store.get(mid)
        assert mem.session_id in ("x", "")

    fts_x = store.fts_search("kubernetes", session_id="x")
    for mid in fts_x:
        mem = store.get(mid)
        assert mem.session_id in ("x", "")


# ─── recall-core parity ───────────────────────────────────────────────────────

def _core_engine(tmp_path):
    core_src = os.path.join(os.path.dirname(__file__), "..", "recall-core", "src")
    sys.path.insert(0, core_src)
    from recall_core import SQLiteStore as CoreStore, RecallEngine, SimpleEmbedder
    store = CoreStore(str(tmp_path / "core.db"))
    return RecallEngine(store, SimpleEmbedder(768)), store


def test_core_score_and_session(tmp_path):
    engine, store = _core_engine(tmp_path)
    engine.add_memory("docker deploy notes for project alpha", session_id="alpha")
    engine.add_memory("docker deploy notes for project beta", session_id="beta")
    results = engine.search("docker deploy", k=10, session_id_filter="alpha")
    assert results
    for m in results:
        assert m.session_id in ("alpha", "")
    assert all(m.score > 0 for m in results)


# ─── sync daemon ──────────────────────────────────────────────────────────────

def test_sync_local(tmp_path):
    server_src = os.path.join(os.path.dirname(__file__), "..", "recall-server", "src")
    core_src = os.path.join(os.path.dirname(__file__), "..", "recall-core", "src")
    sys.path.insert(0, server_src)
    sys.path.insert(0, core_src)
    from recall_server.sync_local import sync_once

    src_db = str(tmp_path / "hermes.db")
    dst_db = str(tmp_path / "server.db")
    state = str(tmp_path / "state.json")

    src = SQLiteStore(src_db)
    _add(src, "hermes remembered docker-compose preference", session_id="s1")
    _add(src, "system background process exited", tag="system")

    stats = sync_once(src_db, dst_db, state_file=state,
                      exclude_tags=["system"])
    assert stats["written"] == 1

    dst = SQLiteStore(dst_db)
    assert dst.count() == 1
    assert "docker-compose" in dst.get_all()[0].content

    # Second run: nothing new
    stats2 = sync_once(src_db, dst_db, state_file=state,
                       exclude_tags=["system"])
    assert stats2["written"] == 0

    # New memory syncs incrementally
    _add(src, "new memory about fastapi endpoints", session_id="s1")
    stats3 = sync_once(src_db, dst_db, state_file=state,
                       exclude_tags=["system"])
    assert stats3["written"] == 1
    assert dst.count() == 2


def test_sync_local_bidirectional(tmp_path):
    server_src = os.path.join(os.path.dirname(__file__), "..", "recall-server", "src")
    sys.path.insert(0, server_src)
    from recall_server.sync_local import sync_once

    src_db = str(tmp_path / "a.db")
    dst_db = str(tmp_path / "b.db")
    state = str(tmp_path / "state.json")

    a = SQLiteStore(src_db)
    b = SQLiteStore(dst_db)
    _add(a, "memory from desktop hermes agent")
    _add(b, "memory written from the mobile app")

    sync_once(src_db, dst_db, state_file=state, bidirectional=True)
    assert SQLiteStore(src_db).count() == 2
    assert SQLiteStore(dst_db).count() == 2


# ─── MCP endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def mcp_client(tmp_path):
    server_src = os.path.join(os.path.dirname(__file__), "..", "recall-server", "src")
    core_src = os.path.join(os.path.dirname(__file__), "..", "recall-core", "src")
    sys.path.insert(0, server_src)
    sys.path.insert(0, core_src)
    from fastapi.testclient import TestClient
    from recall_server.main import create_app
    app = create_app(db_path=str(tmp_path / "mcp.db"))
    return TestClient(app)


def _rpc(client, method, params=None, rid=1):
    return client.post("/mcp", json={
        "jsonrpc": "2.0", "id": rid, "method": method,
        "params": params or {}})


def test_mcp_initialize(mcp_client):
    r = _rpc(mcp_client, "initialize",
             {"protocolVersion": "2025-03-26", "capabilities": {},
              "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["serverInfo"]["name"] == "recall-server"


def test_mcp_tools_list(mcp_client):
    r = _rpc(mcp_client, "tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"recall", "store_memory", "memory_stats"}


def test_mcp_store_and_recall(mcp_client):
    r = _rpc(mcp_client, "tools/call", {
        "name": "store_memory",
        "arguments": {"content": "user prefers dark mode in all apps"}})
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["status"] == "stored"

    r2 = _rpc(mcp_client, "tools/call", {
        "name": "recall", "arguments": {"query": "dark mode preference"}})
    result = json.loads(r2.json()["result"]["content"][0]["text"])
    assert result["count"] >= 1
    assert "dark mode" in result["memories"][0]["content"]


def test_mcp_notification_no_body(mcp_client):
    r = mcp_client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202


def test_api_key_auth(tmp_path):
    server_src = os.path.join(os.path.dirname(__file__), "..", "recall-server", "src")
    sys.path.insert(0, server_src)
    from fastapi.testclient import TestClient
    from recall_server.main import create_app
    app = create_app(db_path=str(tmp_path / "auth.db"), api_key="secret123")
    client = TestClient(app)

    # No key → 401
    assert client.get("/v1/memories?q=x").status_code == 401
    # Status stays public
    assert client.get("/v1/status").status_code == 200
    # X-API-Key works
    assert client.get("/v1/memories?q=x",
                      headers={"X-API-Key": "secret123"}).status_code == 200
    # Bearer works
    assert client.get("/v1/memories?q=x",
                      headers={"Authorization": "Bearer secret123"}).status_code == 200
    # MCP protected too
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}).status_code == 401
