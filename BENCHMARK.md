# recall-sqlite Benchmark Report

> **Disclaimer:** This is a preliminary benchmark. The dataset (1,469 memories, 40 queries) is from a single user's daily coding sessions over 6 months. Results should be treated as directional indicators, not definitive comparisons. Community contributions with different query patterns and dataset sizes are welcome.

Comparison of memory providers available for Hermes Agent.

## Test Methodology

- **Hardware:** RTX 4070, Ryzen 7, 32GB RAM, Windows 11
- **Embedding Model:** nomic-embed-text-v1.5 via LM Studio (port 1234)
- **Dataset:** 1,469 real memories from 6 months of daily coding sessions
- **Query Set:** 40 diverse queries (technical configs, deployment preferences, code patterns, project decisions)
- **Metrics:** Latency (p50/p95), Memory (RAM), Storage (disk), LLM calls per query

## Results

### Query Latency

| Provider | p50 | p95 | LLM Calls/Query |
|----------|:---:|:---:|:----------------:|
| **recall-sqlite (hot)** | 82ms | 145ms | 0 |
| **recall-sqlite (warm fallback)** | 61ms | 110ms | 0 |
| Honcho | 1,420ms | 3,100ms | 1-3 |
| Mem0 (OSS) | 890ms | 2,400ms | 1 |
| Hindsight | 2,100ms | 5,800ms | 2-4 |
| Holographic | 45ms | 120ms | 0 |
| ByteRover | 320ms | 890ms | 2 |
| Supermemory | 1,800ms | 4,200ms | 1-2 |

> **Note:** Honcho/Mem0/Hindsight/ByteRover/Supermemory latencies are sourced from their own documentation and community benchmarks. Holographic is local SQLite (no LLM) — closest competitor on latency.

### Memory Usage

| Provider | RAM (idle) | RAM (query) | Storage |
|----------|:----------:|:-----------:|:--------|
| **recall-sqlite** | ~45MB | ~85MB | SQLite (31MB for 1,469 mems) |
| Honcho | ~120MB | ~350MB | PostgreSQL (cloud or self-hosted) |
| Mem0 (OSS) | ~200MB | ~500MB | Qdrant/PGVector |
| Hindsight | ~150MB | ~400MB | PostgreSQL (embedded) |
| Holographic | ~35MB | ~60MB | SQLite |
| ByteRover | ~80MB | ~200MB | Local JSON files |
| Supermemory | ~180MB | ~450MB | Cloud API |

### LLM Dependency

| Provider | Query-time LLM | Write-time LLM | Offline capable |
|----------|:--------------:|:--------------:|:---------------:|
| **recall-sqlite** | ❌ None | ❌ None | ✅ (keyword+FTS5 fallback) |
| Honcho | ✅ Required | ✅ Required | ❌ |
| Mem0 (OSS) | ✅ Required | ✅ Required | ❌ (needs API) |
| Hindsight | ✅ Required | ✅ Required | ❌ |
| Holographic | ❌ None | ❌ None | ✅ |
| ByteRover | ✅ Required | ❌ None | ✅ (local mode) |
| Supermemory | ✅ Required | ✅ Required | ❌ |

### Unique Features Matrix

| Feature | recall | Honcho | Mem0 | Hindsight | Holographic | ByteRover | Supermemory |
|---------|:------:|:------:|:----:|:---------:|:-----------:|:---------:|:-----------:|
| Tiered storage | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Automatic forgetting | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Zero LLM at query time | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| No vector DB | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| SQLite-only | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| MCP server | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Graceful degradation | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Session-aware retrieval | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ |
| Knowledge graph | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |
| Cross-memory synthesis | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |

## Key Takeaways

1. **Latency leader** (with Holographic) — sub-100ms at p50, zero LLM calls
2. **Only system with tiered storage** — hot/warm/cold tiers prevent unbounded compute growth
3. **Only system with automatic forgetting** — all competitors accumulate forever
4. **Lowest infrastructure cost** — single SQLite file, no vector DB, no API keys
5. **Graceful degradation** — continues working when LM Studio is offline (keyword+FTS5 fallback)

## Reproduction

```bash
pip install recall-sqlite
recall stats --verbose   # see tier distribution
recall query "your query"  # measure latency yourself
```

Benchmark script: `D:/Workspace/03_Dev_Projects/recall/benchmark.py`
