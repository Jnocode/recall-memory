# recall. 🧠

**AI Agent 的輕量級記憶系統。**
純 SQLite 實現的三路 RRF 檢索（ANN + 關鍵字 SQL JOIN + FTS5）。
查詢時不呼叫 LLM。~80ms 延遲。1400+ 條真實記憶。

```python
from recall import retrieve_relevant
store.add("使用者偏好 docker-compose 而非 Dockerfile")
results = retrieve_relevant("該怎麼部署？", store)
# → "使用者偏好 docker-compose 而非 Dockerfile"
```

## 前置需求：Embedding 模型

recall. 使用 **nomic-embed-text-v1.5**（768 維）透過 LM Studio 運行。
不是 LLM — embedding 模型很小（~150MB）、速度快、零 token 成本。

### 1. 安裝 LM Studio

從 [lmstudio.ai](https://lmstudio.ai) 下載。

### 2. 載入 embedding 模型

| 步驟 | 畫面 / 指令 |
|------|-------------|
| 開啟 LM Studio → **Models** 頁籤 | — |
| 搜尋 `nomic-embed-text-v1.5` → 下載 | ~150MB |
| 切換到 **Local Inference Server** 頁籤 | — |
| 在模型下拉選單選擇 `nomic-embed-text-v1.5` | — |
| 點擊 **Start Server** | 預設連接埠 `1234` |
| 驗證是否正常： | `curl http://127.0.0.1:1234/v1/models` |

預期回應：
```json
{"object":"list","data":[{"id":"nomic-embed-text-v1.5","object":"model",...}]}
```

**就這樣。** 不需要 API Key、不需要雲端服務、不需要 GPU（LM Studio 也可以在 CPU 上跑）。

### 連接埠設定

預設連接埠為 `1234`。可在 `src/recall/embed.py` 中更改：
```python
EMBED_PORT = 1234  # 改為你的 LM Studio 連接埠
```

### 如果 LM Studio 離線

系統會自動優雅降級（Graceful Degradation）：

- **檢索**（`mcp_recall_recall` / `recall query`）降級為關鍵字 + FTS5 搜尋 — 不會崩潰，只是沒有 ANN 路徑
- **儲存**（`mcp_recall_store_memory` / `recall add`）不產生 embedding 仍可儲存 — 可透過關鍵字找到
- **CLI**（`recall add/stats/delete`）完全不受影響 — 根本不使用 embedding

無錯誤、無崩潰、無資料遺失。只是結果精準度稍微降低。

---

## 快速開始

```bash
pip install numpy
pip install -e .

recall add "使用者偏好 docker-compose 做本機開發"
recall query "如何部署？"
```

或透過 MCP server 整合到 Hermes Agent / Antigravity IDE / Gemini CLI。

---

## 架構

```text
store.py       — SQLite 後端 + 分層管理
embed.py       — 透過 LM Studio REST API 使用 Nomic Embed（768 維）
retrieve.py    — 三路 RRF 檢索 + 分層路由器
cli.py         — Typer CLI（add / query / stats / delete / gc）
recall_mcp.py  — 供 Agent 整合的 MCP server
```

### 分層儲存（v0.2.0+）

記憶分為三層以降低計算量與記憶體：

| 層級 | 容量 | 檢索方式 | 計算成本 |
|:----|:----:|:---------|:--------:|
| **Hot** | ~500 條 | ANN + 關鍵字 + FTS5（3-path RRF） | 最高 |
| **Warm** | ~5000 條 | 僅關鍵字 + FTS5（2-path RRF） | 中等 |
| **Cold** | 無上限 | 無索引，僅 fill-gap 備用 | 趨近零 |

- **Hot**：完整向量在 ANN 索引中，搜尋最快
- **Warm**：僅關鍵字 / FTS5，無向量，減少 66–99% ANN 計算量
- **Cold**：不參與正常查詢，只在 hot+warm 結果不足時才搜尋

升降級根據存取頻率自動進行。Cold 層每 N 次查詢會抽樣比對關鍵字——若相關則自動升回 Warm。不需 cron、不需 UI、不需設定。

三路平行檢索，透過 RRF（Reciprocal Rank Fusion）融合：

```text
Path V: 向量搜尋 (ANN) — sqlite-vec 餘弦相似度（僅 hot tier）
Path K: 關鍵字 SQL JOIN — 多跳關鍵字擴展（所有 tier）
Path F: FTS5 全文搜尋 — porter tokenizer + unicode61（所有 tier）

分層路由器 → hot 3-path → warm 2-path → cold fill-gap
```

查詢時不呼叫 LLM。不需要向量資料庫。純 SQLite。

---

## 安裝

### 相依套件

| 套件 | 必要？ | 說明 |
|:----|:-----:|:-----|
| Python ≥3.10 | ✅ | — |
| numpy | ✅ | 餘弦相似度 + 向量運算 |
| typer | ✅ | CLI 介面 |
| sqlite-vec | ✅ | SQLite ANN 擴充 |
| LM Studio（port 1234） | ✅ | 運行 nomic-embed-text-v1.5。見上方前置需求 |
| pytest | ❌ | 僅開發需要（`pip install -e ".[dev]"`） |
| sentence-transformers | ❌ | 未使用。實際 embedding 呼叫透過 LM Studio HTTP API |

```bash
pip install numpy
pip install -e .      # 安裝 recall-memory 套件 + sqlite-vec
```

### 驗證安裝

```bash
recall stats
# → Memories: 0  Keywords: 0
```

---

## 使用方式

### CLI

```bash
recall add "內容"                    # 儲存一筆記憶
recall query "問題"                  # 檢索相關記憶（分層）
recall query "問題" --include-cold   # 強制搜尋 cold tier
recall stats                         # 統計資料
recall stats --verbose               # + 顯示 tier 分布
recall gc --dry-run                  # 預覽可清除的記憶
recall gc                            # 執行垃圾清理
recall delete <id>                   # 刪除記憶
```

### MCP 工具（Hermes / Antigravity / Gemini）

| 工具 | 參數 | 回傳值 |
|:----|:-----|:-------|
| `recall` | `query: str`（必填）, `k: int（預設 5）`, `include_cold: bool（預設 false）` | `{memories: [...], count: int}` |
| `store_memory` | `content: str`（必填）, `session_id: str`, `tag: str` | `{id: str, status: "stored"}` |
| `memory_stats` | （無） | `{memories: int, keywords: int, tiers: {hot, warm, cold}}` |
| `gc_memory` | `dry_run: bool（預設 false）` | `{evicted/ candidates: int, db_size_mb: float}` |

---

## 常見問題

### Q: Hardcoded 的 hot/warm 容量限制會不會造成 thrashing（記憶頻繁升降）？

不會。三層防護：

1. **24 小時冷卻期** — 被 demote 的記憶 24 小時內不允許 promote 回去
2. **存取次數門檻** — `access_count ≥ 3` 才觸發 promote
3. **批次操作** — `replenish_hot()` 在寫入時執行，不在查詢路徑上

### Q: Promote/demote 執行到一半時，查詢看到的是什麼狀態？

SQLite WAL mode 保證每次讀取看到的是一個完整的 transaction snapshot。不會有「tier 已改但向量還沒寫完」的中間狀態。

但 promote 不是單一原子操作：
1. `UPDATE tier='hot'` → commit
2. `INSERT vec_embedding` → commit

如果在步驟 1 完成後、步驟 2 完成前 crash，會產生 tier=hot 但無向量的記憶。這條記憶仍然可透過 keyword+FTS5 找到——只是不會出現在 ANN 搜尋結果中，直到重新建立索引。

### Q: 高頻寫入會不會讓 WAL 檔案膨脹到超過 80MB？

這是兩個不同的數字：
- **80MB** 是主 DB 的 eviction 門檻（超過時自動刪除低分記憶），不是 WAL 大小
- **WAL** 是暫存日誌檔；auto-checkpoint（預設約 4MB 時）會自動寫回主 DB 並清空

Promote/demote 不是每次寫入都觸發。只在兩種情況發生：
1. 查詢時冷門記憶被命中（cold→warm promote）
2. 主 DB 超過 80MB（GC demotion）

每次 promote 只有 1-2 個 INSERT/DELETE 敘述，不是幾百個 row。目前 DB 大小為 32MB，離 80MB 門檻還很遠。

### Q: 邊緣端設備上會不會吃掉 SSD 壽命？

每條記憶實際寫入（含所有索引）約 9KB。以每天約 17 條新記憶計算，一年約 56MB。現代 SSD 壽命為數百 TBW——這點寫入量連計算的價值都沒有。

### Q: 高頻寫入下 store() 延遲會不會被 promote/demote 拖累？

目前不會。GC 在 `store()` commit **之後**才檢查 DB 大小（`_gc_if_needed()`），且目前 32MB < 80MB 門檻時只是一個 `stat()` 呼叫（<1ms）。

如果 DB 未來真的超過 80MB，GC 會在 `store()` 回傳前執行 eviction。屆時可以調高門檻或關閉自動 GC，改用手動 `recall gc`。

### Q: 冷卻時間為什麼是 24 小時？

24 小時是保守的預設值，用於防止 thrashing。被 demote 到 cold 的記憶通常代表已經很久沒被 recall 到了——如果它在 24 小時內又變得相關，lazy sampling（每 20 次查詢抽檢）會自動將它 promote 回來。可透過修改 `store.py` 中的 `COOLDOWN_HOURS` 調整。

### Q: 跟 Mem0 / Honcho 比差在哪？

| 面向 | recall-sqlite | Mem0 | Honcho |
|:----|:-------------:|:----:|:------:|
| 查詢時是否呼叫 LLM | 零 | 每次 | 每次 |
| 遺忘機制 | ✅ 自動 tier demotion | ❌ 無 | ❌ 無 |
| 向量資料庫 | 無（SQLite） | Qdrant/PGVector | PostgreSQL |
| 需要 API Key | 否 | 是 | 是 |
| 可離線使用 | ✅（含 graceful fallback） | ❌ | ❌ |
| 資料儲存 | 單一 SQLite 檔 | 自建 | 雲端/自建 |
| p50 延遲 | ~80ms | ~890ms | ~1,420ms |

---

## 狀態

具備分層儲存（v0.2.0）的 production-ready MVP。

```text
記憶數： 1400（從 Honcho 遷移）
關鍵字： 10560
延遲：   ~80ms/查詢 (hot), ~60ms/查詢 (warm fill-gap)
ANN 掃描：-66%（現在）→ -99%（5 萬條記憶時）
記憶體：  ~1.5MB 固定（hot tier）vs 線性成長
```

---

## 升級

### 從 v0.1.x 升級到 v0.2.0

```bash
pip install --upgrade recall-sqlite
```

Schema migration 自動執行——SQLite `ALTER TABLE` 會在第一次啟動時自動運行。
無需手動步驟。既有記憶會被保留並設定為 "hot" tier。

驗證：
```bash
recall stats --verbose
# 應該顯示相同的記憶總數與 tier 分布
```

### 降級

```bash
pip install recall-sqlite==0.1.0
```

---

## 設計決策

| 決策 | 理由 |
|:----|:-----|
| 三路 RRF | ANN + SQL JOIN + FTS5 涵蓋不同的失敗模式 |
| 不使用 LLM re-rank | 額外延遲 + 成本；對檢索品質非必要 |
| SQLite 優先 | 零部署、可攜帶、可 git 版本控制 |
| 透過 LM Studio 使用 Nomic Embed | 768 維、優於 MiniLM、無 Python 套件地獄 |
| RRF 融合 | 無需權重調整；標準 IR 技術 |

---

## 授權條款

Apache 2.0
