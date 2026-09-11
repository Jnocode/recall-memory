# Recall Core — Official Specification (SPEC.md)

> Version: 0.2.0 / 0.3.0 Ready
> Status: Canonical Core Specification
> Conformance: kiro-spec-distillation & memory-architecture-v2

---

## 1. 系統架構定位 (System Architectural Position)

Recall Core (`recall-sqlite`) 是作為跨客戶端單一真相（Single Authority）的語意記憶樞紐核心庫。提供高可靠、低延遲、嵌入式 SQLite 驅動之長期記憶存儲與檢索服務，杜絕跨客戶端（Claude Desktop, ChatGPT, OpenClaw, IDEs）記憶分叉與數據漂移。

```
┌────────────────────────────────────────────────────────────┐
│         Cross-Client Single-Authority Memory Hub           │
├────────────────────────────────────────────────────────────┤
│ Clients: Claude Desktop / ChatGPT / OpenClaw / IDE Plugins │
└─────────────────────────────┬──────────────────────────────┘
                              │ Official MCP / Direct Python API
┌─────────────────────────────▼──────────────────────────────┐
│  Recall Core Engine (`recall-sqlite`)                      │
│  ├── 3-Path RRF Retrieval (ANN 768d + FTS5 + Keyword JOIN) │
│  ├── Multi-Process File-Lock Lease (`authority_lock.py`)   │
│  ├── Scope Namespace Isolation (`project` / `category`)    │
│  └── Soft Delete & Optimistic Concurrency Control (CAS)   │
└────────────────────────────────────────────────────────────┘
```

---

## 2. 檢索核心：3-Path RRF 規範 (Reciprocal Rank Fusion)

Recall Core 採用混合多路檢索與倒數排名融合（Reciprocal Rank Fusion, RRF）架構，融合向量相似度、全文檢索以及結構化關鍵字匹配，解決單一檢索途徑之語意漂移或精確字串遺失問題。

### 2.1 檢索三路徑 (Three Retrieval Paths)

1. **Path 1: Dense Vector ANN (語意相似度)**
   - **向量維度**：標準 768 維度（預設相容 `nomic-embed-text`、`text-embedding-3-small` 等標準嵌入）。
   - **底層驅動**：`sqlite-vec` 虛擬資料表，執行餘弦相似度或歐氏距離近似最近鄰檢索（Cosine/L2 ANN）。
   - **功能**：捕捉意圖相似、跨語種、跨詞彙同義的深層語意。

2. **Path 2: Sparse Full-Text Search (FTS5 全文檢索)**
   - **底層驅動**：SQLite 原生 `fts5` 倒排索引模組。
   - **分詞規範**：支援 unicode61 或 jieba/trigram 分詞，計算 BM25 得分。
   - **功能**：鎖定精確命名、專有名詞、代碼片段、函數名與術語（避免向量空間模糊化）。

3. **Path 3: Keyword / Metadata JOIN (結構化標籤與主題關聯)**
   - **底層驅動**：結構化關聯索引（`tags`, `project`, `subject`, `topic` 欄位）。
   - **功能**：對顯式聲明之專案標籤與主題提供硬性匹配提升（Boost），過濾無關命名空間記憶。

### 2.2 RRF 得分計算公式

三路檢索結果獨立排序後，依據標準 RRF 演算法融合最終排名得分：

$$RRF\_Score(d) = \sum_{m \in \{ANN, FTS5, Keyword\}} \frac{w_m}{k + Rank_m(d)}$$

- **常數平滑係數**：$k = 60$（標準 RRF 倒數常數，防止高排名權重過度陡峭）。
- **權重配置**：$w_{ANN} = 1.0$, $w_{FTS5} = 0.8$, $w_{Keyword} = 0.5$（可依據查詢特性動態微調）。
- **去重與聚合**：候選記錄以 `memory_id` 唯一標識去重，累加各路得分後由大至小排序，截取前 $K$ 筆結果。

---

## 3. 記憶重要性與淘汰策略 (Importance & Eviction Strategy)

為防止長期運行導致知識庫容量膨脹與雜訊污染，Recall Core 內建基於重要性衰減與容量水位的淘汰機制：

1. **記憶重要性維度 (Composite Importance Score)**
   - **基礎置信度 (Base Confidence)**：入庫時給定之權重 $C \in [0.0, 1.0]$。
   - **時間衰減率 (Time Decay)**：半衰期模型 $e^{-\lambda \Delta t}$，其中 $\Delta t$ 為上次存取時間差。
   - **存取頻率增益 (Access Frequency Boost)**：每次被檢索命中並注入上下文時，累加存取計數並重置衰減時鐘。
2. **高水位線淘汰 (High-Watermark Eviction)**
   - 當單一 Scope 記憶筆數超過設定上限（預設 10,000 條）時，觸發批次清理。
   - 優先淘汰：低置信度（Confidence < 0.5）、過期（`valid_until` 到期）且長期未存取之臨時記憶。
   - 釘選保護（Pinned / Immutable）：標記為 `decision` 或重要度滿格（Importance = 1.0）之架構決策享有永久存留保護。

---

## 4. 跨客戶端記憶共享語意 (Cross-Client Concurrency Semantics)

多客戶端（如 OpenClaw 與 Claude Desktop 同時掛載 SQLite DB）必須恪守確定性並發控制語意：

### 4.1 Single Authority 租約鎖定 (Authority Lock)
- 透過 `authority_lock.py` 使用作業系統級檔案鎖（POSIX `fcntl` / Windows `LockFileEx`）實現進程間安全協調。
- 寫入操作必須取得排他鎖（Exclusive Lock），並維持毫秒級 CAS 短事務，嚴禁長事務占用鎖。

### 4.2 Scope 隔離與命名空間 (Scope Isolation)
- 每一筆記憶卡片必須綁定 `project` 或 `category` 標籤。
- 查詢時預設優先返回當前命名空間的精確匹配卡片；`general` 命名空間僅作為後備回退（Fallback），嚴禁跨專案記憶混淆。

### 4.3 軟刪除與樂觀鎖 (Soft Delete & Optimistic Concurrency)
- **軟刪除機制**：所有刪除操作預設標記 `deleted_at = CURRENT_TIMESTAMP`，保留審計歷史並防止並發查詢瞬斷。
- **樂觀並發控制 (CAS)**：記憶版本欄位 `version = version + 1`，更新時校驗版本號，若發生衝突則拒絕寫入並要求客戶端重讀重試。
