# recall. — Retrieval Layer
# Pure vector search with domain vocabulary safety net.
# Feynman verdict: hybrid ≈ pure, entity extraction ≈ noise.

import re
import numpy as np
from datetime import datetime
from typing import Optional

from store import Memory, SQLiteStore
from embed import embed

TOP_K = 10

# ─── Domain vocabulary (safety net, not core) ─────────────────────────────────
DOMAIN_VOCAB = {
    "deploy": ["docker", "docker-compose", "container", "deployment"],
    "deployment": ["docker", "docker-compose", "deploy"],
    "docker": ["docker", "docker-compose", "container", "deployment"],
    "docker-compose": ["docker-compose", "docker", "deploy", "deployment"],
    "hosting": ["ec2", "ecs", "fargate", "cloud", "infrastructure"],
    "ec2": ["ec2", "ecs", "fargate", "migration", "infrastructure"],
    "ecs": ["ecs", "fargate", "ec2", "migration"],
    "fargate": ["fargate", "ecs", "ec2", "migration"],
    "migrate": ["migration", "ec2", "ecs", "fargate"],
    "migration": ["migrate", "ec2", "ecs", "fargate"],
    "infrastructure": ["ec2", "ecs", "fargate", "hosting"],
    "platform": ["ecs", "fargate", "hosting", "infrastructure"],
    "database": ["postgresql", "postgres", "sql", "asyncpg"],
    "postgresql": ["postgresql", "postgres", "database", "sql", "asyncpg"],
    "code": ["type", "hint", "pr", "review", "quality"],
    "review": ["review", "pr", "code", "quality", "type"],
    "type": ["type", "hint", "annotation"],
    "pr": ["pr", "pull", "request", "review", "merge"],
    "api": ["api", "router", "service", "model", "schema", "endpoint", "fastapi"],
    "endpoint": ["endpoint", "api", "router", "fastapi"],
    "fastapi": ["fastapi", "api", "router", "service", "model"],
}


def expand_query(query: str) -> str:
    words = query.lower().split()
    expanded = set(words)
    for w in words:
        clean = w.rstrip("?.,!;:'\"s")
        if clean in DOMAIN_VOCAB:
            expanded.update(DOMAIN_VOCAB[clean])
    return " ".join(expanded)


# ─── ANN ──────────────────────────────────────────────────────────────────────
# ─── Entity extraction (for display only) ────────────────────────────────────

STOP_WORDS = {"the","a","an","is","are","was","were","it","this","that",
    "to","of","in","for","on","with","at","by","from","as","and","or","but",
    "not","be","been","being","have","has","had","do","does","did","will",
    "would","can","could","may","might","shall","should","about","into",
    "through","during","before","after","above","below","between","out",
    "off","over","under","again","further","then","once","here","there",
    "when","where","why","how","all","each","every","both","few","more",
    "most","other","some","such","no","nor","only","own","same","so",
    "than","too","very","just","also","now","get","use","set","put","make",
    "take","come","go","see","know","think","want","give","tell","ask",
    "show","try","leave","call","keep","let","begin","seem","help","turn",
    "what","which","who","whom","whose","where","why","how"}

def extract_entities(text: str) -> list[str]:
    text_lower = text.lower()
    multi_word = re.findall(r'\b[a-zA-Z][a-zA-Z0-9]+[-_][a-zA-Z0-9][-a-zA-Z0-9]*\b', text)
    capitalized = re.findall(r'\b[A-Z][a-zA-Z0-9+#_-]{2,}\b', text)
    versions = re.findall(r'\b[A-Za-z]+\s*\d+\.\d+[\w.]*\b', text)
    camel_case = re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z0-9]*\b', text)
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text_lower)
    words = [w for w in words if w not in STOP_WORDS]
    all_terms = set()
    for term in multi_word + capitalized + versions + camel_case:
        t = term.lower().rstrip('s')
        if t not in STOP_WORDS:
            all_terms.add(t)
    for w in words:
        all_terms.add(w)
    return sorted(all_terms)[:15]

def ann_search(store: SQLiteStore, query_embedding: list[float], k: int = 20) -> list[str]:
    if not store.vec_available or not store.count():
        return []
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(store.db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    vec_bytes = np.array(query_embedding, dtype=np.float32).tobytes()
    rows = conn.execute(
        "SELECT id, distance FROM vec_embeddings WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (vec_bytes, k)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ─── Main retrieval (pure vector + domain vocab) ─────────────────────────────

def retrieve_relevant(
    query: str,
    store: SQLiteStore,
    k: int = TOP_K,
    tag_filter: Optional[str] = None,
) -> list[Memory]:
    # Apply domain vocab expansion
    query_embedding = embed(expand_query(query))

    # ANN first, fallback brute force
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

    # Rank by cosine similarity
    scored = []
    for mem in all_memories:
        if mem.embedding:
            a, b = np.array(query_embedding), np.array(mem.embedding)
            sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))
            scored.append((sim, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]


def pure_vector_search(query: str, store: SQLiteStore, k: int = TOP_K) -> list[Memory]:
    """Pure vector search, no domain vocab expansion. For baseline comparison."""
    query_embedding = embed(query)
    all_memories = store.get_all()
    scored = []
    for mem in all_memories:
        if mem.embedding:
            a, b = np.array(query_embedding), np.array(mem.embedding)
            sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))
            scored.append((sim, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]
