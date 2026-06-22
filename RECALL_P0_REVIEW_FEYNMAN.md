# recall. P0/P0.5 審查報告 — 費曼視角

> 「你不能欺騙自己——而你正是最容易欺騙自己的人。」
> 審查員：理查·費曼（基於程式碼、DB 資料、eval 報告，非二手敘述）

---

## 0. 重大發現：任務描述與磁碟資料完全矛盾

| 資料來源 | Hybrid R | Pure R | 宣稱結果 |
|---------|---------|--------|---------|
| **任務描述給的數字** | 0.561 | 0.270 | hybrid 好 2.08x ✅ |
| **eval_report.md 實際數字** | **0.125** | **0.379** | **hybrid 差 3x ❌** |

我已實際讀取 SQLite DB（recall_p0.db 有 33 筆記憶），驗證 eval_report.md 不是暫存檔或舊版——它是唯一的評估報告，內容與 DB 一致。

**結論：任務描述中「2.08x improvement」這數字不存在於磁碟上。實際結果是相反的。**

---

## 1. P0/P0.5 有沒有過度工程？

### 一言以蔽之：**是的，有個嚴重的問題。**

P0 本身就是一個檔案 264 行——不算過度工程。P0.5 拆分為 4 個檔案 372 行，還在可接受範圍內。

**真正的過度工程不在程式碼行數，而在：**

### 🔴 Cargo Cult #1：實體提取是「聽起來很厲害」的 NLP

看一下實際從記憶中提取的「實體」：

```
"User mentioned they dislike Kubernetes for small projects"
→ entities: ['dislike', 'kubernetes', 'mentioned', 'projects', 'small']
```

「dislike」「mentioned」「small」「projects」是實體？不，它們只是常見英文字詞，因為出現在句子開頭（首字母大寫）或單純是形容詞就被抓進來了。

正確的實體應該是：`Kubernetes`——就這樣。頂多加個 `small projects` 如果是固定詞組。

真正的實體 vs 雜訊：

| 記憶內容 | 提取的實體 | 真正有用的實體 |
|---------|-----------|-------------|
| User prefers docker-compose over Dockerfile for local development | compose, development, docker, **dockerfile**, **local** | docker-compose, Dockerfile |
| User mentioned they dislike Kubernetes for small projects | **dislike**, kubernetes, **mentioned**, **projects**, **small** | Kubernetes |
| The API response time degraded after adding Redis cache layer | **adding**, api, **cache**, **degraded**, **layer** | Redis, API |

這不是實體提取——這是帶停用詞的詞袋模型加上大小寫啟發式。**名字聽起來像 NLP，但本質上只是 `re.findall` 加上 `set()`。**

### 🔴 Cargo Cult #2：entity_overlap_score 公式數學上反直覺

```python
def entity_overlap_score(query_entities, memory_entities):
    intersection = query_entities & memory_entities
    return len(intersection) / max(len(query_entities), len(memory_entities))
```

假設：
- Query 實體：{Kubernetes, Docker} → 2 個
- Memory A 實體：{Kubernetes} → 1 個
- Memory B 實體：{Kubernetes, API, cache, latency, database, pool, deploy, config} → 8 個

結果：
- Memory A 得分：1/2 = 0.50
- Memory B 得分：1/8 = 0.125

**擁有較多資訊的記憶反而被扣分。** 這不是實體重疊——這是「越貧乏越好」的反向獎勵。

### 🔴 時間衰減在 33 筆同一 session 的資料中沒有意義

所有 33 筆記憶在同一 session 內寫入（timestamp 差異可能只有幾分鐘到幾小時）。`recency_score` 對它們全部回傳接近 1.0 的值。所以 0.3 的權重只是在所有結果上加了**均勻的常數**——沒有區分力，純粹稀釋語義分數。

### 🔴 P0.5 的模組化重構做了，但假設還沒驗證

P0.5 把單一檔案拆成 store/embed/retrieve/cli，程式碼多了 41%。這在假設成立時是合理的準備工作——但在假設未驗證時，就是在為一個可能被廢棄的專案建基礎設施。

---

## 2. 2.08x 這個結果可信嗎？還是雜訊？

**不可信。因為它不存在。**

實際資料顯示：**Hybrid R=0.125 vs Pure R=0.379**——純向量搜尋是 hybrid 的 3 倍好。

### 為什麼 hybrid 這麼差？逐層分析失敗原因：

### 失敗原因 1：實體雜訊汙染了分數

Hybrid 有 0.2 的權重來自實體重疊。但實體提取充滿雜訊，所以實體重疊分數測量的是「query 和記憶有多少相同的一般無意義詞」，而不是「query 和記憶有多少相同的重要實體」。

範例：Query「What Docker decisions were made?」
- 提取實體：`['docker', 'decisions', 'made']` ←「decisions」和「made」不是實體
- 這剛好與很多記憶有「decisions」重疊，但它們可能完全無關

### 失敗原因 2：entity_overlap_score 公式雙重打擊

公式 `intersection / max(len(query), len(memory))` 同時有兩個問題：
1. 分母用 max 而不是 union——這不是 Jaccard，而是「懲罰豐富記憶」
2. 沒有實體的記憶自動得 0 分——但實體提取抓到的往往是雜訊而非訊號

### 失敗原因 3：評估集太小且測試不當

20 題，33 筆記憶。平均每個問題約有 2.5 筆 ground truth 記憶（總數 33 筆中約 50 筆 ground truth 標註，分散在 20 題）。

以這樣的樣本量，R=0.125 和 R=0.379 的差距可能只是 noise，但方向很一致——**hybrid 在 20 題中有 13 題拿 0 分，pure vector 只有 7 題拿 0 分。** 這 6 題的差距不太可能只是巧合。

### 實際可信任的結論

| 宣稱 | 實際 | 可信度 |
|-----|------|-------|
| Hybrid > Pure 2.08x | Pure > Hybrid 3x | ❌ 宣稱是反的 |
| R=0.561 | R=0.125 | ❌ 宣稱是反的 |
| 核心假設已驗證 | 核心假設被 falsify | ❌ 需要重新審視 |
| 可以進 P1 | 應該先 Debug 再決定 | ❌ 不能直接進 P1 |

---

## 3. P1 規劃有沒有走歪？哪些可以砍？

### P1 的原始規劃

> 15 檔案, SQLite + sqlite-vec + typer + pydantic v2
> Keyword extraction 優化, 寫入/讀取管線, 簡單遺忘, LangChain integr.

### 費曼診斷：P1 行走的方向有根本性問題

**問題不在 P1 的細節，而在 P1 的前提假設是錯的。**

原 P1 假設 P0 成功了（hybrid > pure），所以才要建更完整的架構 15 個檔案、加 sqlite-vec、加 LangChain 整合。

但 P0 實際結果是 hybrid << pure。所以正確的下一步不是擴建，而是：
1. **先搞懂為什麼 hybrid 比 pure vector 差**
2. **修正根本問題後重新驗證假設**
3. **假設成立後才擴建為正式架構**

### 具體的 P1 修正版（P1-Fix）

#### 第 1 週：除錯實體提取（最優先，不可跳過）

先回答：實體提取到底在提取什麼？

- ✅ 做 Ground truth 實體標註：從 33 筆記憶中人工標出真正的實體（每筆 ~2-5 個）
- ✅ 比較自動提取 vs 人工標註的 precision/recall
- ✅ 如果 P/R < 0.5（極可能），重寫 extract_entities：
  - 先用 spacy 或 stanza 做 POS tagging + NER（比從零寫 regex 更可靠）
  - 或至少改進 regex：不要抓句子開頭的普通詞、加入詞性過濾
- ✅ 修正 entity_overlap_score：改為 `intersection / len(query_entities)` 或 Jaccard

#### 第 2 週：重新評估

- ✅ 沿用原 33 筆記憶 + 20 題，重新跑 hybrid vs pure vector
- ✅ 如果 hybrid 仍然 <= pure：承認 entity 信號權重 0.2 不夠，或根本層是假設錯了
- ✅ 做 ablation study（逐層移除：只有 semantic, semantic+recency, semantic+entity, 全 hybrid）
- ✅ 找到 hybrid 在哪類 query 上勝出，在哪類上失敗

#### 第 3 週：根據結果做決策

有三種可能結果，對應三種路徑：

**結果 A**：修正實體後 hybrid > pure（差距 > 10%）
- → 原 P1 可以走，但規模砍半（7-8 個檔案，不要 15 個）
- → 先不要碰 LangChain（P2 的事）
- → sqlite-vec 可以加，但鎖死版本

**結果 B**：修正後 hybrid ≈ pure（差距 < 10%）
- → Entity signal 0.2 權重太低，試 0.3 或 0.4
- → 如果仍然拉不開，承認：在 all-MiniLM-L6-v2 的 embedding 空間中，semantic 已經包含了大部分實體資訊
- → 決定：不要 hybrid 了，用 pure vector + 更好的 embedding model 解決
- → P1 改成：換大一點的 embedding model（如 all-mpnet-base-v2），看 recall 能不能從 0.379 拉高

**結果 C**：修正後 hybrid 仍然明顯 < pure
- → Hybrid 這條路的方向錯了。所有 hybrid 邏輯砍掉
- → P1 改成：專注在 pure vector 的 scaling（sqlite-vec index、批次 embedding、session filtering）
- → 不用 P0.5 的 retrieve.py，直接簡化

#### 明確排除的項目（費曼版）

| 項目 | 狀態 | 理由 |
|-----|------|------|
| LangChain integration | ❌ 砍掉 | P2 的事，現在整合只是在 problem framing 上面蓋 abstraction |
| 15 個檔案 | ❌ 縮減為 7-8 個 | 15 個檔案是預設 hybrid 成功才需要的複雜度 |
| sqlite-vec | ⚠️ 保留但延後 | 等確認 hybrid 方向正確再整合，否則只是白接 |
| pydantic v2 schema | ✅ 保留 | 資料驗證與假設無關，好的工程習慣 |
| 簡單遺忘 (FIFO + 摘要) | ⚠️ 延後 | 連 retrieval 都還沒搞定就談 forgetting，順序錯了 |
| Keyword extraction 優化 | ✅ 保留 | 這是直擊根本問題的工作，需要做 |

---

## 4. 最終判定

### P0 判定：❌ 不通過

**原因**：P0 的單一成功條件是「hybrid > pure? → YES」。實際結果是「hybrid < pure」，條件未滿足。

### P0.5 判定：⚠️ 有條件通過

**原因**：P0.5（模組化重構）作為工程練習是合理的，但它在假設未驗證時就做了。給條件：
- P0.5 的 store.py/embed.py 可以留著（SQLite CRUD + embedding 包裝是通用基礎設施）
- 但 retrieve.py（hybrid scoring 邏輯）的假設需要重新驗證後才能留

### P1 方向建議：走修正路線 P1-Fix（見上）

**不要直接走原 P1 的 15 檔案 + LangChain 路線。** 那是建立在一個被 falsify 的假設之上的建築。先用 3 週釐清 hybrid 到底行不行，再決定要擴建還是轉彎。

### 船長提醒

> 在物理學中，當實驗結果與理論預測不符時，你不會去建更大的加速器——你會先檢查儀器有沒有接對。
> 
> 這裡的「儀器」就是 extract_entities() 和 entity_overlap_score()。
> 
> 在確認儀器接對之前就規劃 15 個檔案 + LangChain + sqlite-vec——這正是我說的 cargo cult：在還沒理解「為什麼」之前就模仿「怎麼做」。
>
> 先修儀器，再跑實驗，然後看實驗結果告訴你往哪走。

---

## 附錄：實體提取問題範例

以下是 recall_p0.db 中人工檢視的實體品質抽樣：

```
記憶：「User prefers docker-compose over Dockerfile for local development」
提取實體：compose, development, docker, dockerfile, local
良好實體：docker-compose, Dockerfile
雜訊：compose（歧義）, development（太泛）, local（太泛）

記憶：「The API response time degraded after adding Redis cache layer」
提取實體：adding, api, cache, degraded, layer
良好實體：Redis, API, cache
雜訊：adding（動詞）, degraded（過去式）, layer（太泛）

記憶：「Team agreed on trunk-based development with short-lived feature branches」
提取實體：trunk-based, development, branching
良好實體：trunk-based
雜訊：development, branching（抽象名詞）
```

結論：實體提取至少有 40-50% 是雜訊。在雜訊比率這麼高的情況下，任何以實體為基礎的分數都是在隨機打分數。
