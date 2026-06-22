# recall. — Domain Vocab 審查報告

> 基於實際執行的實驗結果、程式碼審查、以及 Feynman P0 審查報告
> 日期：2026-06-22

---

## 一、現狀速覽：兩個評估，不同的故事

### 評估 A：P0 標準基準（20 題，33 筆記憶）

| 方法 | Recall | Precision | vs Pure |
|------|--------|-----------|---------|
| Pure Vector (baseline) | 0.389 | 0.200 | 1.00x |
| Hybrid (entity + recency) | 0.385 | 0.200 | **0.99x** |
| Hybrid v4 (no recency, 0.6/0.0/0.4) | 0.398 | 0.210 | **1.03x** |

**結論：Hybrid ≈ Pure。實體信號沒有幫助。** 包含 domain vocab 的 Hybrid 也只到 1.03x。

### 評估 B：P2 真實場景測試（5 題）

| 方法 | 總分 | 部署場景 | 其他 4 場景 |
|------|------|---------|-----------|
| Baseline (semantic+entity) | 4/5 | ❌ deploy | ✅ |
| **Exp A (domain vocab)** | **5/5** | **✅ deploy** | ✅ |
| Exp B (keyword hybrid) | 4/5 | ❌ deploy | ✅ |
| Exp A+B (combined) | 5/5 | ✅ deploy | ✅ |

**結論：Domain vocab 是唯一成功拉回「部署」recall 的方法。**

---

## 二、問題 1：Domain vocab 是 valid improvement 還是 cargo cult？

### 它是 cargo cult 的證據

1. **字典條目 = 從測試題目手動提煉的答案**
   - 字典知道 "deploy" → "docker docker-compose deployment"
   - 字典知道 "migrate" → "migration ec2 ecs fargate"
   - 這 5 個場景是 de facto 從字典反推的 golden path

2. **P0 標準評估中 domain vocab 沒被測試**
   - 在 `retrieve.py` 中 domain vocab 已經整合到 `retrieve_relevant()`
   - 但 `eval.py`（20 題基準）依然在跑，hybrid ≈ pure（0.99x）
   - Domain vocab 的影響在 20 題基準上幾乎不可見

3. **只解決了「小模型語義空白」這個症狀，不是病因**
   - all-MiniLM-L6-v2 只有 384-dim，對領域概念的編碼很不夠
   - "deploy" 和 "docker-compose" 的 embedding 距離太遠，domain vocab 只是手動補上這條捷徑

### 但問題是真實存在的

讓我們看場景 1 的實際 embedding 行為：
```
Query: "How should I deploy the app?"
→ embedding 向量只編碼了 "deploy" + 語法雜訊
→ 跟 "User prefers docker-compose over Dockerfile" 的距離很遠
→ 所以 baseline 把更相關的 FastAPI/PostgreSQL 記憶排到前面
```

從實際輸出來看：
```
BASELINE 1. Deploy method ❌
  1. FastAPI project structure...  ← 不相關
  2. User uses PostgreSQL...        ← 不相關
  3. We migrated from EC2 to ECS... ← 不相關
→ docker-compose 偏好掉到 4+ 名外

EXP A: Domain vocab 1. Deploy method ✅
  1. User prefers docker-compose... ← ✅ 命中
  2. FastAPI project structure...
  3. User uses PostgreSQL...
→ "deploy" → "deploy deployment docker docker-compose container"
  讓 query embedding 偏移到包含 "docker" 和 "docker-compose" 的空間
```

**這不是「作弊」——這是 bridge the semantic gap。** 如果 embedding model 本身就能編碼 "deploy ≈ docker-compose"，你做 domain vocab 是多餘的。但 all-MiniLM-L6-v2 做不到這件事。

### 綜合判定：**Valid improvement for the WRONG reason**

| 面向 | 判定 |
|------|------|
| 5 題測試中有效 | ✅ Yes — 確實修復了部署 recall |
| 根本原因是 embedding model 不夠強 | ✅ 是 — 換大 model 就自然解決 |
| Manually crafted per scenario | ✅ 是 — 字典針對這 5 題生產 |
| 換領域要重寫 | ✅ 是 — 字典不轉移 |
| 概念上 valid？ | ⚠️ 半套。對的方向（domain adaptation），錯誤的實現（手工字典） |

---

## 三、問題 2：換領域（金融、醫療）要重寫字典嗎？

**要。而且更糟。**

### 目前的字典覆蓋範圍

```
20 entries → 全在 infrastructure/deploy 領域
涵蓋：docker, ec2, ecs, fargate, postgresql, fastapi, pr/review
覆蓋率：正好對應 5 個測試場景
```

### 金融領域（舉例）
- "trade" → trade, execution, settlement, clearing, bid, ask, spread, margin, leverage, position...
- 光 trade settlement 就需要 10+ 條目
- 每個條目需要精確的同義詞鏈——寫錯了反而 degrade 結果

### 醫療領域（舉例）
- "diagnosis" → diagnosis, icd, cpt, ehr, emr, patient, symptom, finding, assessment, procedure...
- 而且 HIPAA 相關的實體提取規則完全不同

### 可以想像的規模

| 領域 | 預估字典大小 | 是否需要領域專家？ |
|------|------------|-----------------|
| 部署/基礎設施 | 20-30 | 不需要（知道的都知道） |
| 金融交易 | 100-200 | 需要（settlement 流程） |
| 醫療 | 200-500 | 需要（臨床編碼、法規） |
| 法律 | 100-300 | 需要（判例法、法條引用） |

Domain vocab 的維護開銷是 **O(domain)** 的——寫入時便宜，但每個新領域都要從頭做。

---

## 四、問題 3：該繼續投資還是收手？

### 不該繼續投資的方向

1. **❌ 擴充字典到 100+ 條目** — 這是一條到處打補丁的路
2. **❌ 用 LLM 自動生成 domain vocab** — 只是把你的手工勞動自動化，但費用變高且難以驗證
3. **❌ 把 domain vocab 包裝成完整架構功能** — 這是安慰劑功能

### 該繼續投資的方向

1. **✅ 保留現有 domain vocab 20 條** — 它確實有用，且有 0 維護成本（寫死在 retrieve.py 中）
2. **✅ 快速驗證「更好的 embedding model 能否自動解決相同問題」** — 如果換 model 後 domain vocab 變得 0 影響，那就證明問題在 model 不在字典
3. **✅ 如果真的要做 domain adaptation** — 用正規方法（見下方「真正解決方案」）

### 決定樹

```
┌─ 換 embedding model（如 bge-base-en-v1.5 或 bigger MiniLM）後：
│
├─ Domain vocab 影響消失 → domain vocab 可以廢棄，問題在 model
│
├─ Domain vocab 仍然有幫助 → 考慮正規 domain adaptation
│   （Fine-tune embedding model on 你的 domain corpus）
│
└─ Hybrid 仍然 ≈ Pure → 砍掉 entity hybrid，只用 pure vector + better model

       ┌── nomic-embed-text-v1.5 already available (LM Studio port 1234)
       │   直接換成它跑 eval
       ▼
   決策點：3 行程式碼、5 分鐘的工作
```

---

## 五、問題 4：真正的解決方案是什麼？

### Root Cause 分析

```
小 model (384-dim) → embedding 空間有限 → 領域概念編碼不足
        ↓
Query "deploy" 跟 memory "docker-compose" 距離過遠
        ↓
Pure vector 找不到正確記憶
        ↓
Hybrid scoring 也救不了（實體提取 40-50% 雜訊，Feynman 確認）
        ↓
Domain vocab 是唯一能 bridge 的方法
        ↓
但它是手工的、不可擴展的、不轉移的
```

### 真正的解決方案（由淺入深）

#### 方案 1：立刻可做（5 分鐘）— 換 Embedding Model

已經有的 infrastructure：LM Studio 跑 nomic-embed-text-v1.5（768-dim, port 1234）

```python
# embed.py 中切換 model 來源
# 從 local sentence-transformers 改成 LM Studio API
# 300+ 維度增加 → 領域概念編碼更好
# 不需要 domain vocab 也能橋接 "deploy" ↔ "docker-compose"
```

風險：nomic-embed-text-v1.5 在 20 題基準上只從 0.385 進步到 0.398（weight tuning report），但 nomic 也是通用 embedding。要測試它對「領域特定」概念的編碼能力。

更好的候選：
- **BAAI/bge-base-en-v1.5**（768-dim, 中文也支持）
- **intfloat/e5-mistral-7b-instruct**（4096-dim，但要顯卡）
- **sentence-transformers/all-mpnet-base-v2**（768-dim, 比 MiniLM 好很多）

#### 方案 2：短期（1-2 天）— 修實體提取 + Domain-specific Fine-tuning

Feynman 已經指出問題：
- 實體提取 40-50% 是雜訊（"dislike", "mentioned", "small" → 不是實體）
- entity_overlap_score 公式懲罰豐富的記憶
- recency 在單一 session 數據中無意義

操作：
1. 用 spacy 或 stanza 取代 regex-based entity extraction（NER 品質大幅提升）
2. 修正 entity_overlap_score：`intersection / len(query_entities)` 而不是 `intersection / max(len(Q), len(M))`
3. 如果 entity 信號仍然無效，接受「hybrid scoring 對小 model 沒用」——砍掉

#### 方案 3：中期（1 週）— Structured Domain Knowledge

如果有領域知識庫（如可編程的 ontology / knowledge graph）：
- 不是手工寫同義詞，而是從現有領域 corpus 用 TF-IDF / PMI 自動挖掘 term 關聯
- 或者用 DSIR 選出 domain-relevant 的 embedding 訓練數據做 domain-adaptive pretraining

這比 domain vocab 更貴，但可擴展。不建議現在做（P0 還不配）。

#### 方案 4：長遠 — Hybrid Scoring + Better Model

```
score = 0.6 × semantic (better embedding) 
      + 0.0 × recency (移除，在 MVP 階段無意義)
      + 0.4 × entity (修復後)
```

如果方案 1 成功（新 model 下 domain vocab 是冗餘的），那終極架構就是：
- Better embedding model → 自動橋接語義 gap
- Fixed entity extraction → 有區分力的實體信號
- Domain vocab 作為可選的 domain adaptation 工具（非核心路徑）

---

## 六、結論

| 問題 | 答案 | 行動 |
|------|------|------|
| Domain vocab valid？ | 半套有效 — 解決真實問題但用錯方法 | 保留 20 條不動，不擴充 |
| 換領域要重寫？ | 是，開銷 O(domain) | 不把 domain vocab 當核心功能 |
| 繼續投資？ | 不要擴充字典 | 投資更好的 embedding model |
| 真正解決方案？ | 換 bigger embedding model + 修 entity extraction | 5 分鐘切到 LM Studio + nomic |

### 立即行動（5 分鐘）

最快的驗證實驗：
1. 將 `embed.py` 從 local all-MiniLM-L6-v2 改為 LM Studio nomic-embed-text-v1.5
2. 跑 `python3 eval.py`（20 題基準）
3. 比較：新 model 下 domain vocab 還有影響嗎？
4. 如果 pure vector recall 已經拉高到 hybrid 水準 → domain vocab 可以 retire

### 最終判定

**Domain vocab 是 transition hack，不是架構選擇。** 它在小型 embedding model + 領域術語缺口的情況下是一個合理的 band-aid。但把它當成正規功能來投資，就是 cargo cult——因為你投資的是測試題的答案，不是系統的檢索能力。
