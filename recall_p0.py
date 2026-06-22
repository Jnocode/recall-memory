# recall. P0 Prototype
# 300 lines, one function: retrieve_relevant()
# Hybrid scoring: semantic + recency + entity_overlap
# Target: prove hybrid > pure vector

import sqlite3
import hashlib
import json
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import re
from sentence_transformers import SentenceTransformer

# ─── Config ───────────────────────────────────────────────────────────────────

EMBED_MODEL = "all-MiniLM-L6-v2"
DB_PATH = "recall_p0.db"
RECENCY_HALF_LIFE_DAYS = 14  # weight halves every 14 days
TOP_K = 10
SCORE_WEIGHTS = {"semantic": 0.5, "recency": 0.3, "entity": 0.2}

# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class Memory:
    content: str
    id: str = ""
    entities: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    embedding: Optional[list[float]] = None
    access_count: int = 0
    session_id: str = ""
    tag: str = "episodic"  # episodic | semantic

# ─── Embedding ────────────────────────────────────────────────────────────────

_model: Optional[SentenceTransformer] = None

def get_embedder():
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBED_MODEL)
    return _model

def embed(text: str) -> list[float]:
    return get_embedder().encode(text, normalize_embeddings=True).tolist()

# ─── Entity Extraction ────────────────────────────────────────────────────────

STOP_WORDS = {"the", "a", "an", "is", "are", "was", "were", "it", "this", "that",
              "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
              "and", "or", "but", "not", "be", "been", "being", "have", "has",
              "had", "do", "does", "did", "will", "would", "can", "could",
              "may", "might", "shall", "should", "about", "into", "through",
              "during", "before", "after", "above", "below", "between",
              "out", "off", "over", "under", "again", "further", "then",
              "once", "here", "there", "when", "where", "why", "how",
              "all", "each", "every", "both", "few", "more", "most",
              "other", "some", "such", "no", "nor", "only", "own",
              "same", "so", "than", "too", "very", "just", "also", "now",
              "的", "是", "了", "在", "有", "我", "不", "這", "那", "也",
              "和", "就", "你", "都", "要", "會", "可", "以", "為", "上"}

def extract_entities(text: str) -> list[str]:
    """Simple keyword extraction: nouns, technical terms, proper names."""
    text_lower = text.lower()
    # Extract capitalized words (potential proper nouns / tech terms)
    capitalized = re.findall(r'\b[A-Z][a-zA-Z0-9+#_-]{1,}\b', text)
    # Extract technical patterns (camelCase, snake_case, version numbers)
    tech_terms = re.findall(r'\b[a-z]+[A-Z][a-zA-Z0-9]*\b', text)  # camelCase
    tech_terms += re.findall(r'\b[a-z]+_[a-z]+\b', text)            # snake_case
    tech_terms += re.findall(r'\b[A-Za-z]+\d+\.\d+\b', text)        # versions
    # Extract single words (filter stop words)
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text_lower)
    words = [w for w in words if w not in STOP_WORDS]
    
    all_terms = set(w.lower() for w in capitalized + tech_terms + words)
    return sorted(all_terms)[:20]  # max 20 entities per memory

# ─── Memory Store ─────────────────────────────────────────────────────────────

class MemoryStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    entities TEXT DEFAULT '[]',
                    timestamp TEXT NOT NULL,
                    embedding BLOB,
                    access_count INTEGER DEFAULT 0,
                    session_id TEXT DEFAULT '',
                    tag TEXT DEFAULT 'episodic'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tag ON memories(tag)")
    
    def add(self, memory: Memory) -> str:
        if not memory.id:
            memory.id = hashlib.md5(memory.content.encode()).hexdigest()[:12]
        memory.embedding = embed(memory.content)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO memories 
                   (id, content, entities, timestamp, embedding, access_count, session_id, tag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (memory.id, memory.content, json.dumps(memory.entities),
                 memory.timestamp.isoformat(), json.dumps(memory.embedding),
                 memory.access_count, memory.session_id, memory.tag)
            )
        return memory.id
    
    def get_all(self) -> list[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM memories ORDER BY timestamp DESC").fetchall()
        return [self._row_to_memory(r) for r in rows]
    
    def get_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    
    def _row_to_memory(self, row) -> Memory:
        return Memory(
            id=row[0], content=row[1], entities=json.loads(row[2]),
            timestamp=datetime.fromisoformat(row[3]),
            embedding=json.loads(row[4]) if row[4] else None,
            access_count=row[5], session_id=row[6], tag=row[7]
        )

# ─── Scoring ──────────────────────────────────────────────────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

def recency_score(timestamp: datetime) -> float:
    days_ago = (datetime.utcnow() - timestamp).days
    return 2 ** (-days_ago / RECENCY_HALF_LIFE_DAYS)

def entity_overlap_score(query_entities: set, memory_entities: set) -> float:
    if not query_entities or not memory_entities:
        return 0.0
    intersection = query_entities & memory_entities
    return len(intersection) / max(len(query_entities), len(memory_entities))

# ─── Retrieval Core — the one function ──────────────────────────────────────

def retrieve_relevant(
    query: str,
    store: MemoryStore,
    k: int = TOP_K,
    weights: Optional[dict] = None
) -> list[Memory]:
    """
    The single function that P0 exists to validate.
    
    Given a query and a memory store, returns the top-k most relevant memories
    using hybrid scoring (semantic + recency + entity overlap).
    
    Returns memories sorted by hybrid score descending.
    """
    if weights is None:
        weights = SCORE_WEIGHTS
    
    query_embedding = embed(query)
    query_entities = set(extract_entities(query))
    all_memories = store.get_all()
    
    if not all_memories:
        return []
    
    scored = []
    for mem in all_memories:
        # Layer 1: Semantic similarity
        sem_score = cosine_similarity(query_embedding, mem.embedding) if mem.embedding else 0.0
        
        # Layer 2: Recency
        rec_score = recency_score(mem.timestamp)
        
        # Layer 3: Entity overlap
        mem_entities = set(mem.entities)
        ent_score = entity_overlap_score(query_entities, mem_entities)
        
        # Hybrid score
        hybrid = (
            weights["semantic"] * sem_score +
            weights["recency"] * rec_score +
            weights["entity"] * ent_score
        )
        scored.append((hybrid, sem_score, rec_score, ent_score, mem))
    
    # Sort by hybrid score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    
    return [mem for _, _, _, _, mem in scored[:k]]

def pure_vector_search(query: str, store: MemoryStore, k: int = TOP_K) -> list[Memory]:
    """Baseline: pure semantic search, no recency or entity scoring."""
    query_embedding = embed(query)
    all_memories = store.get_all()
    
    scored = []
    for mem in all_memories:
        sem_score = cosine_similarity(query_embedding, mem.embedding) if mem.embedding else 0.0
        scored.append((sem_score, mem))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]

# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    store = MemoryStore()
    
    if len(sys.argv) > 1 and sys.argv[1] == "add":
        content = " ".join(sys.argv[2:])
        mem = Memory(
            content=content,
            entities=extract_entities(content),
            session_id="p0_demo"
        )
        store.add(mem)
        print(f"✅ Added memory {mem.id}: {content[:60]}...")
        print(f"   Entities: {mem.entities}")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "query":
        query = " ".join(sys.argv[2:])
        print(f"\n🔍 Query: {query}")
        print(f"   Entities: {extract_entities(query)}")
        
        hybrid_results = retrieve_relevant(query, store)
        pure_results = pure_vector_search(query, store)
        
        print(f"\n📊 Hybrid Retrieval (top {len(hybrid_results)}):")
        for i, mem in enumerate(hybrid_results, 1):
            print(f"  {i}. [{mem.id[:8]}] {mem.content[:80]}...")
        
        print(f"\n📊 Pure Vector Retrieval (top {len(pure_results)}):")
        for i, mem in enumerate(pure_results, 1):
            print(f"  {i}. [{mem.id[:8]}] {mem.content[:80]}...")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        count = store.get_count()
        print(f"📊 Memory stats: {count} memories")
        if count > 0:
            memories = store.get_all()
            print(f"   Latest: {memories[0].content[:60]}...")
            print(f"   Oldest: {memories[-1].content[:60]}...")
    
    else:
        print("Usage:")
        print("  python recall_p0.py add <content>    — Add a memory")
        print("  python recall_p0.py query <text>     — Query memories")
        print("  python recall_p0.py stats            — Show stats")
