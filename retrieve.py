# recall. — Retrieval Layer
# Hybrid scoring: semantic + recency + entity overlap

import re
import numpy as np
from datetime import datetime
from typing import Optional

from store import Memory, SQLiteStore
from embed import get_embedder

# ─── Default weights ──────────────────────────────────────────────────────────

DEFAULT_WEIGHTS = {"semantic": 0.6, "recency": 0.0, "entity": 0.4}
RECENCY_HALF_LIFE_DAYS = 14
TOP_K = 10

# ─── Entity extraction ────────────────────────────────────────────────────────

STOP_WORDS = {
    "the","a","an","is","are","was","were","it","this","that",
    "to","of","in","for","on","with","at","by","from","as",
    "and","or","but","not","be","been","being","have","has",
    "had","do","does","did","will","would","can","could",
    "may","might","shall","should","about","into","through",
    "during","before","after","above","below","between",
    "out","off","over","under","again","further","then",
    "once","here","there","when","where","why","how",
    "all","each","every","both","few","more","most",
    "other","some","such","no","nor","only","own",
    "same","so","than","too","very","just","also","now",
    "get","use","set","put","make","take","come","go","see",
    "know","think","want","give","tell","ask","show","try",
    "leave","call","keep","let","begin","seem","help","turn",
    "的","是","了","在","有","我","不","這","那","也",
    "和","就","你","都","要","會","可","以","為","上",
    "what","which","who","whom","whose","where","why","how",
}


def extract_entities(text: str) -> list[str]:
    """Extract technical terms and meaningful nouns from text.
    
    Priority: multi-word terms > capitalized proper nouns > single words.
    """
    text_lower = text.lower()
    
    # Multi-word technical terms (docker-compose, multi-stage, etc.)
    multi_word = re.findall(r'\b[a-zA-Z][a-zA-Z0-9]+[-_][a-zA-Z0-9][-a-zA-Z0-9]*\b', text)
    
    # Capitalized words (proper nouns, technologies: Docker, FastAPI, React)
    capitalized = re.findall(r'\b[A-Z][a-zA-Z0-9+#_-]{2,}\b', text)
    
    # Versions (Python 3.12, v2, 0.8.0)
    versions = re.findall(r'\b[A-Za-z]+\s*\d+\.\d+[\w.]*\b', text)
    
    # CamelCase terms (PostgreSQL, FastAPI, SQLAlchemy)
    camel_case = re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z0-9]*\b', text)
    
    # Regular words (min 4 chars, not stop words)
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text_lower)
    words = [w for w in words if w not in STOP_WORDS]
    
    # Combine, deduplicate, lowercase, filter stop words
    all_terms = set()
    for term in multi_word + capitalized + versions + camel_case:
        t = term.lower().rstrip('s')
        if t not in STOP_WORDS:
            all_terms.add(t)
    for w in words:
        all_terms.add(w)
    
    return sorted(all_terms)[:15]


# ─── Scoring functions ────────────────────────────────────────────────────────

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


# ─── Retrieval ────────────────────────────────────────────────────────────────

def ann_search(store: SQLiteStore, query_embedding: list[float], k: int = 20) -> list[str]:
    """ANN search via sqlite-vec. Returns memory IDs sorted by distance."""
    if not store.vec_available or not store.count():
        return []
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(store.db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    vec_bytes = np.array(query_embedding, dtype=np.float32).tobytes()
    rows = conn.execute(
        "SELECT id, distance FROM vec_embeddings WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        (vec_bytes, k)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def retrieve_relevant(
    query: str,
    store: SQLiteStore,
    k: int = TOP_K,
    weights: Optional[dict] = None,
    tag_filter: Optional[str] = None,
) -> list[Memory]:
    if weights is None:
        weights = DEFAULT_WEIGHTS

    query_embedding = get_embedder().encode(query, normalize_embeddings=True).tolist()
    query_entities = set(extract_entities(query))

    # Try ANN first, fall back to brute force
    ann_ids = ann_search(store, query_embedding, k=k * 4)
    if ann_ids:
        all_memories = []
        for mid in ann_ids:
            mem = store.get(mid)
            if mem:
                if tag_filter and mem.tag != tag_filter:
                    continue
                all_memories.append(mem)
    else:
        all_memories = store.get_all()
        if tag_filter:
            all_memories = [m for m in all_memories if m.tag == tag_filter]

    if not all_memories:
        return []

    scored = []
    for mem in all_memories:
        sem = cosine_similarity(query_embedding, mem.embedding) if mem.embedding else 0.0
        rec = recency_score(mem.timestamp)
        ent = entity_overlap_score(query_entities, set(mem.entities))
        hybrid = weights["semantic"] * sem + weights["recency"] * rec + weights["entity"] * ent
        scored.append((hybrid, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]


def pure_vector_search(
    query: str,
    store: SQLiteStore,
    k: int = TOP_K,
) -> list[Memory]:
    query_embedding = get_embedder().encode(query, normalize_embeddings=True).tolist()
    all_memories = store.get_all()
    scored = []
    for mem in all_memories:
        sem = cosine_similarity(query_embedding, mem.embedding) if mem.embedding else 0.0
        scored.append((sem, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]
