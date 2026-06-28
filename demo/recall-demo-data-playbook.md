# recall-sqlite Demo 資料呈現方式 — 誠實但有力

> 審計基準：2026-06-23 代碼審計 + CLI 輸出比對  
> 作者：Hermes Agent  
> 原則：DATA SOURCE INTEGRITY — 不編造數值，不偽造 CLI 輸出

---

## 一、總表：每個資料點的審計結果

| # | 資料點 | 狀態 | 說明 |
|---|--------|------|------|
| 1 | 記憶總數（57 條） | ✅ 可用 | 實際 DB 值 |
| 2 | 全部 Hot tier（Warm=0, Cold=0） | ✅ 可用 | 實際 DB 值，須附說明 |
| 3 | CLI query 輸出 | ✅ 可用 | 實際執行 3 個 query |
| 4 | CLI stats 輸出 | ✅ 可用 | 實際執行 |
| 5 | per-path scores（ANN vs Keyword vs FTS5） | ❌ 不能用 | CLI 無此輸出，RRF 分數是內部值 |
| 6 | latency timer（query 側） | ⚠️ 需加 disclaimer | 可引用 BENCHMARK.md 數據，但非即時量測 |
| 7 | tier 分布圖（Hot/Warm/Cold） | ⚠️ 需加 disclaimer | 目前 Warm=0 Cold=0，可展示 schema 設計 |
| 8 | 「零 token 成本」claim | ⚠️ 需加 disclaimer | Query time 確實零 token，但 embedding inference 有計算成本 |
| 9 | 與 Mem0/Honcho 對比表 | ✅ 可用 | BENCHMARK.md 已有對比，須揭露 scope |
| 10 | 三層 RRF 架構圖 | ✅ 可用 | 純架構說明，無造假風險 |
| 11 | 自動遺忘機制 | ✅ 可用 | 代碼中存在 promote/demote/cooldown |
| 12 | Graceful degradation | ✅ 可用 | embed.py 有明確的 try/except |

---

## 二、逐項分析

### 1. 記憶總數（57 條）✅

**來源：** `recall_p0.db` memories 表 `COUNT(*)`  
**呈現方式：** 直接顯示  
**文字：** `57 memories stored in a single SQLite file`

### 2. 全部 Hot tier（Warm=0, Cold=0）✅（但需補充說明）

**實際狀況：** DB 中所有記憶的 tier 皆為 'hot'（這很正常，因為：
- 新 DB 寫入少，還未觸發任何 promote/demote
- `HOT_CAPACITY = 500`，目前僅 57 條，遠未達上限
- GC 門檻是 80MB，目前 DB 約 2MB 左右）

**誠實呈現方式（三選一）：**

**選項 A（推薦）：「實際狀態 + 未來行為」雙欄**
```
現狀:                       設計:
Hot:  57  (全部記憶)        Hot:   ≤500 (full ANN, 3-path RRF)
Warm: 0                     Warm:  ≤5000 (keyword+FTS5, 2-path)
Cold: 0                     Cold:  unlimited (fill-gap only)
                            
# 目前記憶量小，全部視為 hot。當記憶數超過 500 或 DB 超過 80MB，
# promote/demote 會自動啟動。
```

**選項 B：只用 schema 展示（不做 tier 分布圖）**
```sql
CREATE TABLE memories (
    tier TEXT DEFAULT 'hot',   -- hot / warm / cold
    access_count INTEGER DEFAULT 0,
    last_demoted_at TEXT,
    ...
);
```
加上一段 config 常數的展示。

**選項 C：展示預期行為（強制加 disclaimer）**
```
Tier Distribution (預期行為 — 非即時資料):

Hot   [██████████████░░░░░░░░]  57 / 500   ← 3-path: ANN + keywords + FTS5
Warm  [░░░░░░░░░░░░░░░░░░░░░░]   0 / 5000  ← 2-path: keywords + FTS5 only
Cold  [░░░░░░░░░░░░░░░░░░░░░░]   0 / ∞     ← Fill-gap fallback only

⚠️ 目前所有記憶皆為 hot tier（新 DB 狀態）。此圖展示的是系統設計的
    tier 容量與行為，並非當前實際分布。當記憶數增長後，promote/demote
    pipeline 會自動分配記憶至對應 tier。
```

### 3. CLI Query 輸出 ✅

**實際 CLI 輸出格式（來自 `cli.py`）：**
```
🔍 Query: How to deploy?
────────────────────────────────────────────────────────────
  1. [H] [06-24] [episodic] User prefers docker-compose for local dev...
  2. [H] [06-23] [episodic] migration from EC2 to ECS Fargate...
```

**規則：**
- ✅ 可以直接用 terminal 截圖或 code block 展示真實輸出
- ✅ 可加灰色註解行 `# ← 實際 CLI 輸出` 來說明
- ❌ 不可修改 CLI 輸出內容（包括不能加 per-path scores、latency badges 在 terminal 內）
- ✅ 可在 **terminal 之外**（SVG 的資訊框/annotation 區）加補充數據

### 4. CLI Stats 輸出 ✅

**實際輸出（來自 `cli.py`）：**
```
📊 recall. stats
──────────────────────────────
  Total:     57
  Episodic:  42
  Semantic:  15
```

（如果加 `--verbose`：）
```
  Tier Distribution:
    Hot:   57
    Warm:  0
    Cold:  0
```

✅ 可直接展示  
✅ 可加 `recall stats` + `recall stats --verbose` 的對比來突顯 tier 欄位存在

### 5. per-path scores ❌ 不能用

**為什麼不行：** RRF score 是 `retrieve.py` 內部的中間變數，CLI 從未輸出。  
RRF fusion 是標準 IR 技術，scores 本身無視覺意義（1/(60+rank) 的加權值）。

**替代方案：** 展示三條 path 的**代碼**，而不是 scores。

```python
# retrieve.py — Three-path RRF fusion

# Path V: Vector search (ANN) — hot tier only
vec_ids = ann_search(store, query_embedding, k=k*3)

# Path K: Keyword SQL JOIN — multi-hop expansion
kw_ids = store.search_by_keywords(expanded_keywords, limit=k*3, tier=tier)

# Path F: FTS5 full-text search
fts_ids = store.fts_search(query, limit=k*3, tier=tier)

# RRF: 1/(60+rank) per result per path
```

**架構示意圖（terminal 外）：**
```
Query: "How to deploy?"
         │
    ┌────┼────┐
    ▼    ▼    ▼
   ANN  KW   FTS5    ← Three paths
    │    │    │
    └────┼────┘
         ▼
    RRF Fusion      ← Reciprocal Rank Fusion
         │
         ▼
  3 results returned
```

### 6. Latency timer ⚠️ 需加 disclaimer

**實際可用的 latency 數據（來自 BENCHMARK.md）：**

| Provider | p50 | p95 | LLM Calls/Query |
|----------|:---:|:---:|:----------------:|
| recall-sqlite (hot) | **82ms** | **145ms** | 0 |
| recall-sqlite (warm fallback) | **61ms** | **110ms** | 0 |
| Honcho | 1,420ms | 3,100ms | 1-3 |
| Mem0 | 890ms | 2,400ms | 1 |

**來源：** 1400 筆記憶 × 40 queries 的 benchmark，跑在 RTX 4070 / Ryzen 7 / Win11。  
**Disclaimer 文字：**
```
⚠️ Latency data sourced from BENCHMARK.md (1400 memories × 40 queries).
   Your mileage may vary based on hardware, embedding model, and DB size.
   This is NOT a real-time measurement of this exact session.
```

**禁止事項：**
- ❌ 在 CLI terminal 截圖內加虛構的 `[82ms]` badge
- ❌ 在 query 輸出中插入 latency timer 行
- ✅ 可在 terminal 外的資訊卡 / 對比表中呈現 benchmark 數據

### 7. Tier 分布圖 ⚠️ 需加 disclaimer

**請見第 2 點。** 重點總結：
- 顯示 schema 設計而非當前分布 → 加分
- 若顯示圖表，必須標註「預期行為」而非「當前狀態」

### 8. 「零 token 成本」claim ⚠️ 需加 disclaimer

**實際情況拆解：**

| 階段 | Token 成本 | 計算成本 | 說明 |
|------|:----------:|:--------:|------|
| Write (embed) | 0 token | ✅ ~50-150ms LM Studio | embedding inference，不是 LLM |
| Write (store) | 0 token | ✅ ~5-15ms SQLite | INSERT into 4 tables |
| Query (search) | **0 token** | ✅ | RRF 全部是 SQL + 向量計算 |
| Query (LLM re-rank) | N/A | ✅ | recall 從不做 LLM re-rank |

**誠實說法（原本）：**
> Zero LLM calls at query time. No LLM re-rank. Just SQLite.

→ ✅ 這是真實的。

**膨風說法（不可用）：**
> Zero token cost. Free memory.
> → ❌ 忽略了 embedding 的計算成本

**推薦用語：**
> **Query-time zero LLM.** 只用 SQLite 計算（cosine similarity + keyword JOIN + FTS5），不需要任何 LLM 調用。寫入時的 embedding 使用 150MB 的 nomic-embed-text-v1.5（非 LLM），不做 API 調用、不消耗 tokens。

### 9. 與 Mem0/Honcho 對比 ✅

**來源：** BENCHMARK.md + README.md  
**可以呈現的對比維度：**

| Aspect | recall-sqlite | Mem0 | Honcho |
|--------|:-------------:|:----:|:------:|
| Query-time LLM | **Zero** | Every call | Every call |
| Forgetting mechanism | ✅ Auto tier demotion | ❌ None | ❌ None |
| Vector DB | None (SQLite) | Qdrant/PGVector | PostgreSQL |
| API Key required | No | Yes | Yes |
| Offline capable | ✅ (graceful fallback) | ❌ | ❌ |
| Storage | Single SQLite file | Self-hosted | Cloud/self-hosted |
| p50 latency | **~80ms** | ~890ms | ~1,420ms |

**必須加 disclaimer：**
```
⚠️ 對比數據來自 BENCHMARK.md，使用相同 embedding model (nomic-embed-text-v1.5)。
   Honcho/Mem0 latency 來自其官方文件與社群 benchmark。recall-sqlite 的
   優勢在於 query-time 無 LLM，這直接反映在 latency 差異上。
```

### 10. 三層 RRF 架構圖 ✅

**安全邊界內：**
- ✅ 用 SVG/ASCII 畫架構圖：輸入 → 三條 path → RRF → 輸出
- ✅ 標註每條 path 使用的技術（sqlite-vec / SQL JOIN / FTS5）
- ✅ 標註 tier router 邏輯（hot: 3-path → warm: 2-path → cold: fill-gap）
- ❌ 不可在圖上標虛構的 latency 或 scores

### 11. 自動遺忘機制 ✅

**來源：** store.py 中的 real code  
**可以展示：**

```python
# Real code from store.py

PROMOTION_THRESHOLD = 3   # access_count needed for warm→hot
COOLDOWN_HOURS = 24       # min hours after demotion before re-promotion
HOT_CAPACITY = 500        # max memories with ANN vectors
WARM_CAPACITY = 5000      # max memories with keyword/FTS5 only

def trim_hot(self):
    """Demote lowest-access-count hot memories to warm."""
    if self.count_tier("hot") <= HOT_CAPACITY:
        return 0  # no action needed

def sample_cold_for_promotion(self, query_keywords):
    """Every 20 queries, check cold memories for relevance."""
    # Random sample → keyword overlap check → promote if ≥20% match
```

### 12. Graceful Degradation ✅

**來源：** `embed.py` + `retrieve.py`  
**安全展示：**

```python
# embed.py — LM Studio failure handling
def embed(text):
    try:
        resp = urllib.request.urlopen(...)
        return data["data"][0]["embedding"]
    except Exception:
        return None  # ← graceful: caller degrades to 2-path

# retrieve.py — ANN fallback
if not embed_available:
    # Graceful: skip ANN, use keyword + FTS5 only
    kw_ids = store.search_by_keywords(...)
    fts_ids = store.fts_search(...)
```

---

## 三、SVG 視覺增強規則

### 可以做的事 ✅

| 類別 | 範例 | 位置 |
|------|------|------|
| 灰色註解行 | `# 實際 CLI 輸出 — 57 條記憶，全部 hot tier` | terminal output 下方 |
| 資訊框 | 對比表、架構說明、code snippets | terminal 外部（badge 區） |
| 圖標 | 資料庫 icon、green checkmark、badge | SVG 結構元素 |
| 流程圖 | 三層 RRF 架構圖、tier router 流程 | 區塊示意圖 |
| 色碼 | 統一配色、highlight key numbers | 全局設計 |
| Disclaimer 標籤 | ⚠️ 字樣 + 說明文字 | 貼近有疑慮的資料點 |

### 不可做的事 ❌

| 類別 | 範例 | 原因 |
|------|------|------|
| 修改 CLI 輸出 | 在 terminal 截圖內加文字 | 等同偽造 CLI 輸出 |
| 虛構 scores | per-path scores、RRF 細項分數 | CLI 無此輸出 |
| 虛構 latency | 在 query 旁標 `82ms` | 非即時量測 |
| 偽造 tier 分布 | 畫出 Warm=120 Cold=45 的餅圖 | 實際 Warm=0 Cold=0 |
| 偽造 timeline | 虛構的 query log 時間戳 | 等同偽造數據 |

### 設計範例：terminal + 資訊框版面

```
┌─────────────────────────────────────────────────────┐
│  $ recall query "How to deploy?"                    │
│  ─────────────────────────────────────────           │
│    1. [H] [06-24] [episodic] User prefers...         │
│    2. [H] [06-23] [episodic] migration from...       │
│    3. [H] [06-22] [episodic] Docker compose...       │
│  # 實際 CLI 輸出，57 條記憶中的 top-3               │
└─────────────────────────────────────────────────────┘
┌─────────────────┐  ┌─────────────────────────────────┐
│ ⚡ Zero LLM      │  │ Three-Path RRF:                 │
│ Query time =     │  │ 🔵 ANN (sqlite-vec)              │
│  純 SQLite 計算  │  │ 🟢 Keyword SQL JOIN              │
│                   │  │ 🟠 FTS5 full-text search         │
│ p50: ~80ms       │  │ → RRF fusion (no LLM re-rank)   │
│ (BENCHMARK.md)   │  └─────────────────────────────────┘
└─────────────────┘
```

---

## 四、三個 Query 的展示素材

基於實際 DB 中的 57 條記憶，建議執行的 3 個 query：

| # | Query | 預期結果類型 | 展示重點 |
|---|-------|-------------|---------|
| 1 | `recall query "Docker deployment"` | 部署偏好相關記憶 | 語義檢索 + keyword expansion |
| 2 | `recall stats --verbose` | 統計資訊 + tier 欄位 | 證明 tier schema 存在 |
| 3 | `recall query "project architecture"` | 專案架構相關記憶 | 跨 session 檢索 |

**每個 query 執行後，以 code block 收錄真實輸出，附加灰色註解。**  
**SVG 製作時：** 在 terminal 區塊外補充說明文字。

---

## 五、總結：呈現策略

| 你的訴求點 | 資料呈現方式 | 誠實度 |
|-----------|-------------|--------|
| 輕量級 | 一個 SQLite 檔案，57 條記憶，~2MB | ✅ 真實 |
| 查詢零 LLM | 展示 `retrieve.py` 代碼，0 個 `openai.ChatCompletion` 調用 | ✅ 真實 |
| 速度快 | 引用 BENCHMARK.md 的 ~80ms p50 | ⚠️ 附 disclaimer |
| 有 tiered storage | 展示 schema 設計 + code，不展示虛構的分布 | ✅ 誠實但有力 |
| 自動遺忘機制 | 展示 `store.py` 的 promote/demote/sampling code | ✅ 真實 |
| 三層檢索 | 畫架構圖，展示三條 path 的 code | ✅ 純事實 |
| 可離線運作 | 展示 `embed.py` 的 graceful try/except | ✅ 真實 |
| 無 API key | 展示零套件依賴 | ✅ 真實 |
