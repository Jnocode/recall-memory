"""Tests for recall. — Memory retrieval for AI agents."""
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from datetime import datetime, timedelta
from recall.store import Memory, SQLiteStore, extract_keywords
from recall.embed import embed


def test_store_crud():
    """Test basic CRUD operations on the store."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        store = SQLiteStore(db_path, vec_dim=768)
        assert store.count() == 0
        
        mem = Memory(content="Test memory content", entities=["test"],
                     timestamp=datetime.utcnow(), session_id="test", tag="episodic")
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
        del store; import gc; gc.collect()
        os.unlink(db_path)


def test_keyword_extraction():
    """Test keyword extraction from content."""
    kws = extract_keywords("User prefers docker-compose for local dev")
    assert "docker-compose" in kws
    assert "docker" in kws or "compose" in kws


def test_embed():
    """Test embedding function returns expected dimension."""
    vec = embed("test query")
    assert len(vec) == 768
    assert all(isinstance(v, float) for v in vec)


def test_store_clear():
    """Test clearing all memories."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        store = SQLiteStore(db_path, vec_dim=768)
        store.add(Memory(content="test", timestamp=datetime.utcnow()))
        assert store.count() == 1
        store.clear()
        assert store.count() == 0
        del store; import gc; gc.collect()
    finally:
        os.unlink(db_path)


def test_multiple_memories():
    """Test storing and retrieving multiple memories."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        store = SQLiteStore(db_path, vec_dim=768)
        for i in range(5):
            store.add(Memory(content=f"Memory {i}", timestamp=datetime.utcnow()))
        assert store.count() == 5
        assert len(store.get_all()) == 5
        del store; import gc; gc.collect()
    finally:
        os.unlink(db_path)


def test_keywords_table():
    """Test that keywords are indexed properly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        store = SQLiteStore(db_path, vec_dim=768)
        mem = Memory(content="Docker compose deployment with Kubernetes",
                     timestamp=datetime.utcnow())
        store.add(mem)
        
        ids = store.search_by_keywords(["docker", "kubernetes"], limit=10)
        assert len(ids) == 1
        del store; import gc; gc.collect()
    finally:
        os.unlink(db_path)
