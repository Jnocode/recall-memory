# recall. — Retrieval Layer
# Three-path RRF retrieval: ANN + keyword SQL JOIN + FTS5.
# Gracefully degrades to 2-path (keyword + FTS5) when LM Studio is unavailable.
# No LLM at query time.

import re
import numpy as np
from typing import Optional

from .store import Memory, SQLiteStore, extract_keywords as extract_entities
from .embed import embed

TOP_K = 10

# ─── Domain vocabulary (safety net, kept per Feynman) ─────────────────────────
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


def expand_query(query: str, max_terms: int = 10) -> str:
    words = query.lower().split()
    expanded = set(words)
    for w in words:
        clean = w.rstrip("?.,!;:'\"s")
        if clean in DOMAIN_VOCAB and len(expanded) < max_terms:
            expanded.update(DOMAIN_VOCAB[clean])
    return " ".join(expanded)


# ─── ANN ──────────────────────────────────────────────────────────────────────

def ann_search(store: SQLiteStore, query_embedding: list[float], k: int = 20) -> list[str]:
    if not store.vec_available or not store.count():
        return []
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(store.db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    vec_bytes = np.array(query_embedding, dtype=np.float32).tobytes()
    rows = conn.execute(
        "SELECT id FROM vec_embeddings WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (vec_bytes, k)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ─── Three-path retrieval (degrades gracefully) ───────────────────────────────

def retrieve_relevant(
    query: str,
    store: SQLiteStore,
    k: int = TOP_K,
    tag_filter: Optional[str] = None,
    hops: int = 2,
) -> list[Memory]:
    """Three-path RRF retrieval.

    Path V: Vector search (ANN) via sqlite-vec
    Path K: Keyword SQL JOIN with multi-hop expansion
    Path F: FTS5 full-text search

    If LM Studio (embedding) is unavailable, Path V is skipped and the
    system falls back to 2-path retrieval (keyword + FTS5).
    """
    # Try to embed the expanded query; if unavailable, skip ANN paths
    expanded = expand_query(query, max_terms=20)
    query_embedding = embed(expanded) if expanded else None
    embed_available = query_embedding is not None

    if not embed_available:
        # Graceful degradation: try bare query (no expansion) as fallback
        query_embedding = embed(query)

    path_results: dict[str, float] = {}

    # Path V: Vector search (ANN) — only if embedding is available
    if query_embedding is not None:
        vec_ids = ann_search(store, query_embedding, k=k * 3)
        for rank, mid in enumerate(vec_ids):
            path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + rank)

    # Path K: Keyword SQL JOIN (multi-hop)
    seed_ids = list(path_results.keys()) if path_results else []
    kw_ids = set(seed_ids)
    kw_rank = 0
    for hop in range(hops):
        rkws = store.get_related_keywords(seed_ids, limit=15) if seed_ids else []
        new_ids = set(store.search_by_keywords(rkws, limit=k * 3))
        added = new_ids - kw_ids
        kw_ids.update(new_ids)
        seed_ids = list(added)
        for mid in added:
            path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + kw_rank)
            kw_rank += 1
        if not added or hop >= hops - 1:
            break

    # Path F: FTS5 full-text search
    fts_ids = store.fts_search(query, limit=k * 3)
    for rank, mid in enumerate(fts_ids):
        path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + rank)

    # Query keywords also contribute to keyword path
    query_kws = list(set(expanded.split()[:10]))
    qkw_ids = store.search_by_keywords(query_kws, limit=k * 3)
    for rank, mid in enumerate(qkw_ids):
        path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + rank)

    # Filter by tag
    if tag_filter:
        path_results = {
            mid: sc for mid, sc in path_results.items()
            if (mem := store.get(mid)) and mem.tag == tag_filter
        }

    if not path_results:
        all_mems = store.get_all()
        if tag_filter:
            all_mems = [m for m in all_mems if m.tag == tag_filter]
        return _rank_by_embedding(all_mems, query_embedding, k) if all_mems else []
    else:
        scored = []
        for mid, rrf_score in path_results.items():
            mem = store.get(mid)
            if mem and mem.embedding and query_embedding is not None:
                a, b = np.array(query_embedding), np.array(mem.embedding)
                sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))
                scored.append((rrf_score * 0.7 + sim * 0.3, mem))
            elif mem:
                scored.append((rrf_score, mem))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in scored[:k]]


def _rank_by_embedding(
    memories: list[Memory],
    query_embedding: list[float] | None,
    k: int,
) -> list[Memory]:
    """Fallback ranking when no RRF results exist. Sorts by cosine similarity."""
    if query_embedding is None:
        return memories[:k]
    scored = []
    for mem in memories:
        if mem.embedding:
            a, b = np.array(query_embedding), np.array(mem.embedding)
            sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))
            scored.append((sim, mem))
    if not scored:
        return memories[:k]  # fallback: no embeddings to rank by, return latest
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]
