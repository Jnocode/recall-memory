# recall. — Ablation Experiment Results
## 換 model 驗證 domain vocab 假設：已完成

---

## 實驗設計

純 vector search（無 hybrid、無 domain vocab）比較兩個 embedding model 在 5 個場景上的表現。

| Model | Dim | 來源 |
|-------|-----|------|
| all-MiniLM-L6-v2 | 384 | local sentence-transformers |
| nomic-embed-text-v1.5 | 768 | LM Studio (port 1234) |

---

## 結果 1：Top-3 Recall（與 _experiments.py 同樣標準）

| Model | 總分 | 1.Deploy | 2.DB | 3.API | 4.Code | 5.Infra |
|-------|------|----------|------|-------|--------|---------|
| MiniLM pure vector | **4/5** | ❌ | ✅ | ✅ | ✅ | ✅ |
| Nomic pure vector | **5/5** | ✅ | ✅ | ✅ | ✅ | ✅ |
| Nomic + hybrid | **5/5** | ✅ | ✅ | ✅ | ✅ | ✅ |

## 結果 2：Top-1 Recall（更嚴格的標準）

| Model | Top-1 |
|-------|-------|
| MiniLM pure vector | 2/5 |
| Nomic pure vector | **2/5** |
| Nomic + hybrid | **2/5** |

## 結果 3：Semantic Separation（正確 vs 錯誤記憶的相似度差距）

| 場景 | 正確 sim | 平均錯誤 sim | 差距 |
|------|---------|------------|------|
| 1. Deploy | 0.4658 | 0.4418 | **+0.0240** |
| 2. Database | 0.6182 | 0.4700 | **+0.1482** |
| 3. API結構 | 0.5476 | 0.4725 | **+0.0752** |
| 4. Code品質 | 0.4790 | 0.4254 | **+0.0537** |
| 5. Infrastructure | 0.4592 | 0.4747 | **-0.0155** |

---

## 發現

### 1. ✅ Domain vocab IS redundant with better embeddings

Nomic-embed 自動把部署場景救回 top-3：「How should I deploy?」的正確記憶從 MiniLM 的 4+ 名外提升到第 2 名。**不需要 domain vocab。**

### 2. ❌ 但 hybrid scoring 仍然無效

Nomic + entity hybrid vs Nomic pure vector：**完全相同的 5/5 top-3、2/5 top-1。** Entity 信號加了等於沒加——因為 entity extraction 依然充滿雜訊（Feynman 2014 報告確認的 40-50% 雜訊率）。

### 3. ⚠️ Top-1 準確率只有 2/5

5 個場景中只有 2 個的 #1 是正確答案。其他 3 個場景的正確記憶在 #2 或 #3。原因是：

**FastAPI「萬有引力」問題：** 記憶「FastAPI project structure: routers/services/models/schemas/」因為包含太多常見技術詞（"api", "service", "model"），幾乎對所有 query 都是 #1 或 #2。這是一個 embedding 空間「密度太高」的訊號——768-dim 的 nomic 讓所有事物都長得有點像，失去了區分力。

### 4. ⚠️ 部署場景的 margin 只有 +0.024

Query「How should I deploy?」對正確記憶的相似度只比平均錯誤高 2.4%。基礎設施場景甚至錯誤比正確高 1.5%。這表示**這些 margin 在更大的記憶庫中（100+ 筆）會立刻被淹沒。**

---

## 結論

### Domain Vocab 判定

| 結論 | 證據 |
|------|------|
| **Domain vocab 在 nomic-embed 下是冗餘的** | Nomic pure vector 已達 5/5 top-3 |
| **但問題沒有真正解決——只是從「找不到」變成「排名不穩」** | Top-1 只有 2/5，margin 只有 0.02-0.07 |
| **Domain vocab 20 條可以保留但不擴充** | 是便宜的 safety net |

### 真正的下一步

不是 domain vocab，不是 entity hybrid，而是**解決 embedding 空間區分力不足**的問題：

1. **換更好的 embedding model**（done，但還在鋸箭）
2. **Fine-tune embedding model on domain corpus**（讓 "deploy" → "docker-compose" 的距離真正接近）
3. **或者：接受 embedding 不夠強，用 LLM re-rank top-20**（違反原始設計但解決問題）

### 建議的實際行動

- Domain vocab 保留 20 條不動
- 把 `embed.py` 正式切到 LM Studio nomic-embed（768-dim），同時改 `store.py` vec_dim 成 768
- 重新建 eval DB（33 筆記憶重 embedding）
- 放棄 entity hybrid（Feynman 也建議同等結論，hybrid ≈ pure）
- **單純走 pure vector + better embedding 路線**

### 最關鍵的數字

```
MiniLM pure vector (top-3):   4/5  ← "deploy" missing
Nomic pure vector (top-3):    5/5  ← all found
Nomic pure vector (top-1):    2/5  ← wrong #1 for 3/5 scenarios
```

換 model 解決了 recall 的「發現」問題，但還沒解決「排名」問題。這是下一個階段的工作。
