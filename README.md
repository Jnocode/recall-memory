# recall. 🧠
[![Hermes Memory Provider](https://img.shields.io/badge/Hermes-Memory%20Provider-blue)](https://github.com/NousResearch/hermes-agent/pull/51205)
[![PyPI](https://img.shields.io/pypi/v/recall-sqlite)](https://pypi.org/project/recall-sqlite/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

> **🔌 Hermes plugin → [github.com/Jnocode/recall-memory-hermes](https://github.com/Jnocode/recall-memory-hermes)** — 安裝指引與 Hermes Agent 整合設定

<p align="center">
  <a href="https://raw.githubusercontent.com/Jnocode/recall-memory/master/demo/recall-demo-video-narrated.mp4">
    <img src="https://img.shields.io/badge/🎬%20看%20Demo%20影片-58s-blue?style=for-the-badge" alt="Demo Video">
  </a>
</p>

> 👀 **非開發者？** 點上面的按鈕看 58 秒影片，了解 recall 在做什麼。不需要懂程式碼。

**Better contextual retrieval for AI agents.**
Three-path RRF retrieval (ANN + keyword SQL JOIN + FTS5) in pure SQLite.
No LLM at query time. ~80ms latency. 1400 real memories.

```python
from recall import retrieve_relevant
store.add("User prefers docker-compose over Dockerfile")
results = retrieve_relevant("How should I deploy?", store)
# → "User prefers docker-compose over Dockerfile"
```

## Prerequisites: Embedding Model

recall. uses **nomic-embed-text-v1.5** (768-dim) running in LM Studio.
No LLM — embedding models are tiny (~150MB), fast, and cost zero tokens.

### 1. Install LM Studio

Download from [lmstudio.ai](https://lmstudio.ai).

### 2. Load the embedding model

| Step | Screenshot / Cmd |
|------|------------------|
| Open LM Studio → **Models** tab | — |
| Search `nomic-embed-text-v1.5` → Download | ~150MB |
| Switch to **Local Inference Server** tab | — |
| Select `nomic-embed-text-v1.5` in the model dropdown | — |
| Click **Start Server** | port defaults to `1234` |
| Verify it's working: | `curl http://127.0.0.1:1234/v1/models` |

Expected response:
```json
{"object":"list","data":[{"id":"nomic-embed-text-v1.5","object":"model",...}]}
```

**That's it.** No API keys, no cloud services, no GPU required beyond what LM Studio needs (~2GB VRAM, also runs on CPU).

### Port configuration

Default port is `1234`. To change it, set `EMBED_PORT` in `src/recall/embed.py`:
```python
EMBED_PORT = 1234  # change to match your LM Studio port
```

### If LM Studio is down

Graceful degradation kicks in automatically:

- **retrieval** (`mcp_recall_recall` / `recall query`) falls back to keyword + FTS5 search — no crash, just no ANN path
- **storage** (`mcp_recall_store_memory` / `recall add`) saves memories without embeddings — still findable via keywords
- **CLI** (`recall add/stats/delete`) unaffected — doesn't use embeddings at all

No error, no crash, no data loss. Just slightly less precise results.

---

## Quick start

```bash
pip install numpy
pip install -e .

recall add "User prefers docker-compose for local dev"
recall query "How to deploy?"
```

Or via MCP server for Hermes Agent / Antigravity IDE / Gemini CLI:

### Hermes (local install)

If recall is installed in the same Python env as Hermes:
```yaml
# ~/.hermes/config.yaml
mcp_servers:
  recall:
    command: "python"
    args: ["-m", "recall.recall_mcp"]
    timeout: 30
    cwd: "/path/to/recall-memory"   # optional, needed for DB path resolution
```

### Hermes (Docker)

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  recall:
    command: docker
    args:
      - run
      - -i
      - --rm
      - --network=host
      - -v
      - recall-data:/data
      - recall-memory:latest
    timeout: 30
```

Build the image first:
```bash
cd /path/to/recall-memory
docker compose build
```

## Architecture

```text
store.py       — SQLite backend + tier management
embed.py       — Nomic Embed via LM Studio REST API (768-dim)
retrieve.py    — Three-path RRF retrieval + tier router
cli.py         — Typer CLI (add / query / stats / delete / gc)
recall_mcp.py  — MCP server for agent integration
```

### Tiered Storage (v0.2.0+)

Memories are split into three tiers to reduce compute and memory:

| Tier | Capacity | Retrieval | Compute Cost |
|------|:--------:|:----------|:------------:|
| **Hot** | ~500 | ANN + keywords + FTS5 (3-path RRF) | Highest |
| **Warm** | ~5000 | keywords + FTS5 only (2-path RRF) | Medium |
| **Cold** | Unlimited | Not indexed, fill-gap fallback only | ~Zero |

- **Hot**: full vectors in ANN index. Fastest search.
- **Warm**: keyword/FTS5 only, no vectors. 66–99% less ANN work.
- **Cold**: doesn't participate in normal queries. Only searched when hot+warm results are insufficient.

Promotion/demotion is automatic based on access frequency. Cold memories are sampled every N queries for keyword overlap—if relevant, they're promoted back to warm. No cron, no UI, no configuration needed.

Three parallel retrieval paths, fused via RRF (Reciprocal Rank Fusion):

```text
Path V: Vector search (ANN) — sqlite-vec cosine similarity (hot tier only)
Path K: Keyword SQL JOIN — multi-hop keyword expansion (all tiers)
Path F: FTS5 full-text search — porter tokenizer + unicode61 (all tiers)

Tier router → hot 3-path → warm 2-path → cold fill-gap
```

No LLM calls at query time. No vector database. Just SQLite.

## Installation

### Dependencies

| Dependency | Required? | Notes |
|-----------|-----------|-------|
| Python ≥3.10 | ✅ | — |
| numpy | ✅ | Cosine similarity + vector ops |
| typer | ✅ | CLI interface |
| sqlite-vec | ✅ | SQLite extension for ANN |
| LM Studio (port 1234) | ✅ | Runs nomic-embed-text-v1.5. See Prerequisites above. |
| pytest | ❌ | Only needed for development (`pip install -e ".[dev]"`) |
| sentence-transformers | ❌ | Not used. The actual embedding calls go through LM Studio's HTTP API. |

```bash
pip install numpy
pip install -e .      # installs recall-memory package + pulls sqlite-vec
```

### Verify installation

```bash
recall stats
# → Memories: 0  Keywords: 0
```

## Usage

### CLI

```bash
recall add "content"           # Store a memory
recall query "question"        # Retrieve relevant memories (tiered)
recall query "question" --include-cold  # Force search cold tier too
recall stats                   # Store statistics
recall stats --verbose         # + tier distribution
recall gc --dry-run            # Preview eviction candidates
recall gc                      # Run garbage collection
recall delete <id>             # Remove a memory
```

### MCP Tools (Hermes / Antigravity / Gemini)

| Tool | Parameters | Returns |
|------|-----------|---------|
| `recall` | `query: str` (required), `k: int (default 5)`, `include_cold: bool (default false)` | `{memories: [...], count: int}` |
| `store_memory` | `content: str` (required), `session_id: str`, `tag: str` | `{id: str, status: "stored"}` |
| `memory_stats` | (none) | `{memories: int, keywords: int, tiers: {hot, warm, cold}}` |
| `gc_memory` | `dry_run: bool (default false)` | `{evicted/ candidates: int, db_size_mb: float}` |

### Tiered Storage — How It Works

v0.2.0 introduced tiered storage to reduce compute and memory.
Here's what happens under the hood — you don't need to configure anything.

**Query flow:**
```
You: recall query "docker compose"
  → Hot tier (3-path RRF: ANN + keywords + FTS5)     ← ~500 fastest memories
  → Warm tier (2-path RRF: keywords + FTS5 only)     ← ~5000 fallback
  → Cold tier (keywords + FTS5, promoted on hit)      ← everything else
  → Results returned
```

- **Hot**: memories with vector embeddings. ANN search runs here. ~80ms.
- **Warm**: no vectors, but keyword + FTS5 still work. Slightly lower relevance.
- **Cold**: doesn't participate in normal queries. Only used if hot+warm results are insufficient.

**Promotion/demotion happens automatically:**
- A memory you frequently query gets promoted to higher tiers
- Unused memories gradually shift to lower tiers over time
- Cold tier is sampled every 20 queries — if a cold memory's keywords match your query, it gets promoted back to warm

**When to use `--include-cold`:**
If you're searching for something very old or obscure that didn't appear in results, add this flag to force a full scan.

**When to run `gc`:**
Never, unless you care about disk space. Auto-triggers at 80MB DB size.
`recall gc --dry-run` previews what would be deleted.
`recall gc` actually deletes low-score memories (score < 0.5, rarely accessed).

**What tiered storage does NOT change:**
- Query syntax is identical
- No configuration files to edit
- No cron jobs or background processes
- Schema migration is automatic on `pip install --upgrade`

## FAQ

### Q: Can hardcoded hot/warm capacity limits cause thrashing?

No. Three layers of protection:

1. **24h cooldown** — a demoted memory cannot be promoted back for 24 hours
2. **Lifetime threshold** — `access_count ≥ 3` required before promotion triggers
3. **Batch operation** — `replenish_hot()` runs during writes, not on the query path

### Q: What state does a query see while promote/demote is in progress?

SQLite WAL mode guarantees every reader sees a complete snapshot of the transaction as it began. There is no "tier updated but vector not yet written" intermediate state.

However, promote is not a single atomic operation:
1. `UPDATE tier='hot'` → commit
2. `INSERT vec_embedding` → commit

A crash between step 1 and step 2 leaves a tier=hot memory with no vector. This memory is still retrievable via keyword+FTS5 — it just won't appear in ANN search results until reindexed.

### Q: Can frequent writes bloat the WAL file beyond 80MB?

These are two different numbers:
- **80MB** is the eviction threshold for the main DB file (auto-delete low-score memories), not the WAL size
- **WAL** is a temporary journal; auto-checkpoint (~4MB default) flushes it back to the main DB and clears it

Promote/demote does not fire on every write. It triggers in two cases:
1. A cold memory is hit during query (cold→warm promote)
2. The main DB exceeds 80MB (GC demotion)

Each promote is 1-2 INSERT/DELETE statements, not hundreds of rows. The current DB is 32MB — far below the 80MB threshold.

### Q: Will this wear out an SSD on edge devices?

Each memory write (including all indexes) is ~9KB. At ~17 new memories per day, that's ~56MB per year. Modern SSDs are rated for hundreds of TBW — this amount is below the noise floor.

### Q: Does promote/demote slow down store() under heavy writes?

Currently no. GC checks DB size (`_gc_if_needed()`) **after** `store()` commits, and at 32MB < 80MB threshold it's just a `stat()` call (<1ms).

If the DB eventually exceeds 80MB, GC runs eviction before `store()` returns. At that point you can raise the threshold or disable auto-GC and run `recall gc` manually.

### Q: Why 24 hours for the cooldown?

24h is a conservative default to prevent thrashing. A memory demoted to cold was likely not accessed for a long time — if it becomes relevant again within 24 hours, lazy sampling (every 20 queries) will catch it and promote it back. Adjust `COOLDOWN_HOURS` in `store.py` to change.

### Q: How does this compare to Mem0 / Honcho?

| Aspect | recall-sqlite | Mem0 | Honcho |
|--------|:-------------:|:----:|:------:|
| Query-time LLM | Zero | Every call | Every call |
| Forgetting mechanism | ✅ Auto tier demotion | ❌ None | ❌ None |
| Vector DB | None (SQLite) | Qdrant/PGVector | PostgreSQL |
| API Key required | No | Yes | Yes |
| Offline capable | ✅ (graceful fallback) | ❌ | ❌ |
| Data storage | Single SQLite file | Self-hosted | Cloud/self-hosted |
| p50 latency | ~80ms | ~890ms | ~1,420ms |

### Q: Will there be multi-device sync / CRDT support?

Not currently on the roadmap. recall-sqlite is designed as a local-first, single-device memory layer. The SQLite backend is intentionally simple — no conflict resolution, no cloud sync, no distributed locking.

Sync could theoretically be layered on top (SQLite files are portable), but it would require careful handling of concurrent writes across devices. If you need multi-device memory, Honcho or Supermemory are better fits today.

## Status

Production-ready MVP with tiered storage (v0.2.0). Tested against AIngram (tied on 1400 memories × 40 queries).

```text
Memories: 1400 (from Honcho)
Keywords: 10560
Latency:  ~80ms/query (hot), ~60ms/query (warm fill-gap)
ANN scan: -66% (now) → -99% (at 50K memories)
Memory:   ~1.5MB fixed for hot tier vs linear growth
Eval:     recall@5 comparable to AIngram with full extractor
```

## Upgrading

### From v0.1.x to v0.2.0

```bash
pip install --upgrade recall-sqlite
```

Schema migration is automatic — SQLite `ALTER TABLE` runs on first start.
No manual steps needed. Your existing memories are preserved and will start
in the "hot" tier.

To verify:
```bash
recall stats --verbose
# Should show the same memory count with tier distribution
```

### Rollback

```bash
pip install recall-sqlite==0.1.0
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Three-path RRF | ANN + SQL JOIN + FTS5 covers different failure modes |
| No LLM re-rank | Extra latency + cost; not needed for retrieval quality |
| SQLite first | Zero-deployment, portable, git-committable |
| Nomic embed via LM Studio | 768-dim, better than MiniLM, no Python packaging hell |
| RRF fusion | No weight tuning needed; standard IR technique |

## Comparison with AIngram

| System | R@5 (40 mems) | R@5 (1400 mems) | Latency |
|--------|:------------:|:--------------:|:-------:|
| recall. | 0.579 | ~0.58 | ~80ms |
| AIngram | 0.583 | ~0.58 | ~27ms |

Both systems tied on identical embedding model. recall.'s advantage: three-path architecture (AIngram uses two-path when extractor is unavailable).

## License

Apache 2.0
