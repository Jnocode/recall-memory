# recall. 🧠

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

## CLI

```bash
recall add "content"           # Store a memory
recall query "question"        # Retrieve relevant memories (tiered)
recall query "question" --include-cold  # Search cold tier too
recall stats                   # Store statistics
recall stats --verbose         # + tier distribution
recall gc --dry-run            # Preview eviction candidates
recall gc                      # Run garbage collection
recall delete <id>             # Remove a memory
```

## MCP Tools (Hermes / Antigravity / Gemini)

Three tools exposed via stdio MCP transport:

| Tool | Parameters | Returns |
|------|-----------|---------|
| `recall` | `query: str` (required), `k: int (default 5)`, `include_cold: bool (default false)` | `{memories: [...], count: int}` |
| `store_memory` | `content: str` (required), `session_id: str`, `tag: str` | `{id: str, status: "stored"}` |
| `memory_stats` | (none) | `{memories: int, keywords: int, tiers: {hot, warm, cold}}` |
| `gc_memory` | `dry_run: bool (default false)` | `{evicted/ candidates: int, db_size_mb: float}` |

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
