# recall. — Retrieval Layer
# Three-path RRF retrieval: ANN + keyword SQL JOIN + FTS5.
# Tiered storage: hot (3-path), warm (2-path), cold (fallback only).
# Gracefully degrades to 2-path (keyword + FTS5) when LM Studio is unavailable.
# No LLM at query time.

import re
import numpy as np
from typing import Optional
from datetime import datetime, timezone

from .store import Memory, SQLiteStore, extract_keywords, SAMPLING_INTERVAL
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

def ann_search(store: SQLiteStore, query_embedding: list[float],
               k: int = 20) -> list[str]:
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


# ─── Single-tier retrieval ────────────────────────────────────────────────────

def retrieve_relevant(
    query: str,
    store: SQLiteStore,
    k: int = TOP_K,
    tag_filter: Optional[str] = None,
    hops: int = 2,
    tier: Optional[str] = None,
    include_cold: bool = False,
) -> list[Memory]:
    """Three-path RRF retrieval for a single tier.

    Path V: Vector search (ANN) via sqlite-vec (only if tier='hot')
    Path K: Keyword SQL JOIN with multi-hop expansion
    Path F: FTS5 full-text search

    Gracefully degrades to 2-path when ANN is unavailable.
    """
    expanded = expand_query(query, max_terms=20)
    query_embedding = embed(expanded) if expanded else None
    embed_available = query_embedding is not None

    if not embed_available:
        query_embedding = embed(query)

    path_results: dict[str, float] = {}

    # Path V: Vector search (ANN) — only for hot tier
    if query_embedding is not None and (tier is None or tier == "hot"):
        vec_ids = ann_search(store, query_embedding, k=k * 3)
        for rank, mid in enumerate(vec_ids):
            path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + rank)

    # Path K: Keyword SQL JOIN (multi-hop, within tier)
    seed_ids = list(path_results.keys()) if path_results else []
    kw_ids = set(seed_ids)
    kw_rank = 0
    for hop in range(hops):
        rkws = store.get_related_keywords(seed_ids, limit=15) if seed_ids else []
        new_ids = set(store.search_by_keywords(rkws, limit=k * 3, tier=tier))
        added = new_ids - kw_ids
        kw_ids.update(new_ids)
        seed_ids = list(added)
        for mid in added:
            path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + kw_rank)
            kw_rank += 1
        if not added or hop >= hops - 1:
            break

    # Path F: FTS5 full-text search (within tier)
    fts_ids = store.fts_search(query, limit=k * 3, tier=tier)
    for rank, mid in enumerate(fts_ids):
        path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + rank)

    # Query keywords also contribute to keyword path
    query_kws = list(set(expanded.split()[:10]))
    qkw_ids = store.search_by_keywords(query_kws, limit=k * 3, tier=tier)
    for rank, mid in enumerate(qkw_ids):
        path_results[mid] = path_results.get(mid, 0.0) + 1.0 / (60 + rank)

    # Filter by tag
    if tag_filter:
        path_results = {
            mid: sc for mid, sc in path_results.items()
            if (mem := store.get(mid)) and mem.tag == tag_filter
        }

    if not path_results:
        all_mems = store.get_all(limit=k * 10)
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


# ─── Tier Router ──────────────────────────────────────────────────────────────

_tier_router_query_count = 0


def retrieve_tiered(
    query: str,
    store: SQLiteStore,
    k: int = TOP_K,
) -> list[Memory]:
    """Tier-aware retrieval with fill-gap fallback and lazy cold sampling.

    1. Search hot tier first (3-path RRF)
    2. If hot results < k, fill gap from warm (2-path RRF)
    3. If hot+warm still < k, fill gap from cold (keywords + FTS5)
    4. Increment access_count on all returned memories
    5. Every N queries, sample cold tier for promotion
    """
    global _tier_router_query_count

    all_results = []

    # Step 1: Hot tier (3-path RRF)
    if store.count_tier("hot") > 0:
        hot_results = retrieve_relevant(query, store, k=k, tier="hot")
        seen_ids = set(m.id for m in hot_results)
        all_results.extend(hot_results)
    else:
        seen_ids = set()

    # Step 2: Fill gap from warm tier (2-path RRF, no ANN)
    if len(all_results) < k and store.count_tier("warm") > 0:
        needed = k - len(all_results)
        warm_results = retrieve_relevant(query, store, k=needed, tier="warm")
        for m in warm_results:
            if m.id not in seen_ids and len(all_results) < k:
                all_results.append(m)
                seen_ids.add(m.id)

    # Step 3: Fill gap from cold tier (keywords + FTS5 only)
    if len(all_results) < k and store.count_tier("cold") > 0:
        needed = k - len(all_results)
        warm_capacity = WARM_CAPACITY - store.count_tier("warm")
        cold_results = retrieve_relevant(query, store, k=needed, tier="cold")
        for m in cold_results:
            if m.id not in seen_ids and len(all_results) < k:
                all_results.append(m)
                seen_ids.add(m.id)
                # Promote cold→warm only if capacity available and not in cooldown
                if warm_capacity > 0:
                    cooldown_ok = (m.last_demoted_at is None or
                        (datetime.now(timezone.utc) - m.last_demoted_at).total_seconds()
                        >= COOLDOWN_HOURS * 3600)
                    if cooldown_ok:
                        store.promote(m.id, "warm")
                        warm_capacity -= 1

    # Step 4: Increment access_count on returned memories
    for mem in all_results:
        store.increment_access(mem.id)

    # Step 5: Lazy cold sampling (every N queries)
    _tier_router_query_count += 1
    if _tier_router_query_count >= SAMPLING_INTERVAL:
        _tier_router_query_count = 0
        query_kws = extract_keywords(query)
        store.sample_cold_for_promotion(query_kws)

    return all_results


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
        return memories[:k]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k]]
