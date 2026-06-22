"""Tests for recall. — Memory retrieval for AI agents.

Core logic tests use mocks so they run without LM Studio.
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from recall.store import Memory, SQLiteStore, extract_keywords
from recall.embed import embed, is_loaded
from recall.retrieve import retrieve_relevant, expand_query, _rank_by_embedding


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_store(memories: list[tuple[str, str, str]] = None) -> SQLiteStore:
    """Create a temp SQLiteStore with optional memories.

    Each memory: (content, session_id, tag)
    """
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    store = SQLiteStore(db_path, vec_dim=768)
    if memories:
        for content, session_id, tag in memories:
            mem = Memory(content=content, session_id=session_id, tag=tag,
                         timestamp=datetime.now(timezone.utc))
            store.add(mem)
    return store, db_path


def _cleanup(db_path):
    try:
        os.unlink(db_path)
    except Exception:
        pass


# ─── Store CRUD ───────────────────────────────────────────────────────────────

def test_store_crud():
    store, db_path = _make_store()
    try:
        assert store.count() == 0

        mem = Memory(content="Test memory content", entities=["test"],
                     timestamp=datetime.now(timezone.utc),
                     session_id="test", tag="episodic")
        mem_id = store.add(mem)
        assert mem_id is not None
        assert store.count() == 1

        fetched = store.get(mem_id)
        assert fetched is not None
        assert fetched.content == "Test memory content"

        all_mems = store.get_all()
        assert len(all_mems) == 1

        store.delete(mem_id)
        assert store.get(mem_id) is None
    finally:
        _cleanup(db_path)


def test_store_clear():
    store, db_path = _make_store()
    try:
        store.add(Memory(content="test", timestamp=datetime.now(timezone.utc)))
        assert store.count() == 1
        store.clear()
        assert store.count() == 0
    finally:
        _cleanup(db_path)


def test_multiple_memories():
    store, db_path = _make_store()
    try:
        for i in range(5):
            store.add(Memory(content=f"Memory {i}", timestamp=datetime.now(timezone.utc)))
        assert store.count() == 5
        assert len(store.get_all()) == 5
    finally:
        _cleanup(db_path)


# ─── Keywords ─────────────────────────────────────────────────────────────────

def test_keyword_extraction():
    kws = extract_keywords("User prefers docker-compose for local dev")
    assert "docker-compose" in kws
    assert "docker" in kws


def test_keywords_table():
    store, db_path = _make_store()
    try:
        mem = Memory(content="Docker compose deployment with Kubernetes",
                     timestamp=datetime.now(timezone.utc))
        store.add(mem)

        ids = store.search_by_keywords(["docker", "kubernetes"], limit=10)
        assert len(ids) == 1
    finally:
        _cleanup(db_path)


# ─── Expand query ─────────────────────────────────────────────────────────────

def test_expand_query_domain_vocab():
    """Domain vocab expands 'deploy' to include docker-related terms."""
    result = expand_query("How should I deploy?", max_terms=20)
    assert "deploy" in result
    assert "docker" in result or "docker-compose" in result
    assert "deployment" in result


def test_expand_query_unknown_terms():
    """Unknown terms pass through unchanged."""
    result = expand_query("quantum entanglement", max_terms=10)
    assert "quantum" in result
    assert "entanglement" in result


# ─── Embedding ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not is_loaded(), reason="LM Studio not running on port 1234")
def test_embed():
    """Test embedding function returns expected dimension."""
    vec = embed("test query")
    assert vec is not None
    assert len(vec) == 768
    assert all(isinstance(v, float) for v in vec)


# ─── Retrieval (mock-based, no LM Studio needed) ──────────────────────────────

@patch("recall.retrieve.embed")
def test_retrieve_relevant_empty_store(mock_embed):
    """Querying an empty store returns empty results."""
    mock_embed.return_value = [0.0] * 768

    store, db_path = _make_store()
    try:
        results = retrieve_relevant("test query", store, k=5)
        assert len(results) == 0
    finally:
        _cleanup(db_path)


@patch("recall.retrieve.embed")
def test_retrieve_relevant_basic(mock_embed):
    """Basic retrieval returns correct memory by keyword."""
    mock_embed.return_value = [0.0] * 768

    memories = [
        ("User prefers docker-compose over Dockerfile", "session1", "semantic"),
        ("FastAPI project uses routers and services", "session1", "semantic"),
    ]
    store, db_path = _make_store(memories)
    try:
        results = retrieve_relevant("deploy", store, k=5)
        # Should find the deploy-related memory via keyword path
        assert len(results) >= 1
        assert any("docker-compose" in m.content for m in results)
    finally:
        _cleanup(db_path)


@patch("recall.retrieve.embed")
def test_retrieve_relevant_tag_filter(mock_embed):
    """Tag filter narrows results to a specific tag."""
    mock_embed.return_value = [0.0] * 768

    memories = [
        ("Docker deployment steps", "s1", "semantic"),
        ("User said they like Docker", "s1", "episodic"),
    ]
    store, db_path = _make_store(memories)
    try:
        results = retrieve_relevant("Docker", store, k=5, tag_filter="semantic")
        assert len(results) == 1
        assert results[0].tag == "semantic"
    finally:
        _cleanup(db_path)


@patch("recall.retrieve.embed")
def test_retrieve_relevant_graceful_degradation(mock_embed):
    """When embed() returns None, retrieval degrades to keyword + FTS5."""
    mock_embed.return_value = None  # Simulate LM Studio outage

    memories = [
        ("Deploy with docker-compose", "s1", "semantic"),
    ]
    store, db_path = _make_store(memories)
    try:
        results = retrieve_relevant("How to deploy?", store, k=5)
        # Should still find results via keyword/FTS5 even without embedding
        assert len(results) >= 1
    finally:
        _cleanup(db_path)


@patch("recall.retrieve.embed")
def test_retrieve_relevant_multiple_results(mock_embed):
    """Multiple results sorted by relevance."""
    mock_embed.return_value = [0.0] * 768

    memories = [
        ("PostgreSQL connection pool settings", "s1", "semantic"),
        ("Use asyncpg for PostgreSQL", "s1", "semantic"),
        ("Docker compose for local dev", "s1", "semantic"),
        ("React component structure", "s1", "semantic"),
    ]
    store, db_path = _make_store(memories)
    try:
        results = retrieve_relevant("PostgreSQL async pool", store, k=3)
        assert len(results) >= 1
        pg_results = [m for m in results if "PostgreSQL" in m.content or "postgres" in m.content.lower()]
        assert len(pg_results) >= 1
    finally:
        _cleanup(db_path)


# ─── Fallback ranking ─────────────────────────────────────────────────────────

def test_rank_by_embedding_empty_scored():
    """_rank_by_embedding returns latest memories when no embeddings exist."""
    mems = [
        Memory(content="A", timestamp=datetime.now(timezone.utc)),
        Memory(content="B", timestamp=datetime.now(timezone.utc)),
    ]
    result = _rank_by_embedding(mems, None, k=2)
    assert len(result) == 2  # fallback: no query_embedding
    assert result[0].content == "A"

    result2 = _rank_by_embedding(mems, [0.0] * 768, k=2)
    assert len(result2) == 2  # fallback: all memories lack embedding vector
    assert result2[0].content == "A"
