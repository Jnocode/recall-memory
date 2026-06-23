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

### Q: Hardcoded 的 hot/warm 容量限制會不會造成 thrashing（記憶頻繁升降）？

不會。有三層防護：

1. **24h cooldown** — 記憶被 demote 後 24 小時內不允許 promote 回去，避免短時間內反覆升降
2. **Lifetime threshold** — `access_count ≥ 3` 才 promote，不是 recall 一次就升
3. **Batch operation** — 主要的 `replenish_hot()` 不在 query 路徑上做，而是在寫入時觸發

### Q: Promote/demote 執行到一半時，query 看到的是什麼狀態？

SQLite WAL mode 保證 reader 看到每個 transaction 開始時的完整 snapshot。不會有「tier 已改但向量還沒寫完」的中間狀態。

但 promote 不是單一原子操作：
1. `UPDATE tier='hot'` → commit
2. `INSERT vec_embedding` → commit

如果 step 1 後 crash，會產生 tier=hot 但無向量的記憶。但這條記憶仍然可以透過 keyword+FTS5 找到，只是少了向量搜尋的精準度。記憶檢索是機率性的 — 漏掉一條剛 promote 的記憶，下次 query 就會找到。

### Q: 高頻寫入會不會讓 WAL 檔案膨脹到超過 80MB？

不會混淆兩個數字：
- **80MB** 是主 DB 的 eviction 門檻（超過時自動刪除低分記憶），不是 WAL 大小
- **WAL** 是暫存檔，checkpoint（預設 ~4MB 時）自動寫回主 DB 後清空

Promote/demote 不是每寫一條記憶就觸發。目前只在兩種情況發生：
1. 查詢時冷門記憶被命中才 promote（cold→warm）
2. DB 超過 80MB 的 GC 才執行 demote

每次 promote 就一兩個 INSERT/DELETE，不是幾百個 row。目前 DB 才 32MB，離 80MB threshold 還很遠。

### Q: 邊緣端設備上會不會吃掉 SSD 壽命？

每條記憶實際寫入（含所有 index）約 9KB。每天約 17 條新記憶 → 一年約 56MB。現代 SSD 壽命是幾百 TBW — 這點量連算都不值得算。

即使長期累積加上偶爾的 promote/demote，寫入量也在灰塵級別。

### Q: 高頻寫入下 store() 延遲會不會被 promote/demote 拖累？

目前不會。GC 在 `store()` commit **之後**才檢查 DB size（`_gc_if_needed()`），且目前 32MB < 80MB threshold 時只是一個 `stat()` call（<1ms），不影響寫入速度。

長遠如果 DB 超過 80MB，GC 會在 `store()` return 前跑 eviction，屆時可調高 threshold 或關閉自動 GC 改手動 `recall gc`。

### Q: cooldown 時間為什麼是 24 小時？

24 小時是保守預設值，防止短時間內的 thrashing。一條記憶被 demote 到 cold 通常代表它已經很久沒被 recall 到了 — 如果它在 24 小時內又變得相關，自然會被 lazy sampling（每 20 次 query 抽檢）重新 promote 回來。這個值可以通過修改 `store.py` 中的 `COOLDOWN_HOURS` 調整。

### Q: 跟 Mem0 / Honcho 比差在哪？

| 面向 | recall-sqlite | Mem0 | Honcho |
|------|:-------------:|:----:|:------:|
| Query-time LLM | 零 | 每次 call | 每次 call |
| 遺忘機制 | ✅ 自動 tier demotion | ❌ 無 | ❌ 無 |
| 向量 DB | 無（SQLite） | Qdrant/PGVector | PostgreSQL |
| API Key 需要 | 無 | 需 API Key | 需 API Key |
| 離線可用 | ✅（含 graceful fallback） | ❌ | ❌ |
| 資料儲存 | 單一 SQLite 檔 | 自建 | 自建/雲端 |
| p50 latency | ~80ms | ~890ms | ~1,420ms |

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
