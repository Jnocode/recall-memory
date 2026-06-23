# Periodic Promotion Sampling 評估報告

> 評估「Tiered Storage 中 Hot/Warm/Cold 三層架構」是否需要 Periodic Promotion Sampling，
> 以及如何在不引入 cron/UI/背景 process 的前提下實作。

---

## 1. 場景分析：Cold 記憶「被擠掉」的機率

### 提問中的 scenario

```
recall 有 10,000 條記憶，Hot=500, Warm=5000, Cold=4500
使用者問一個問題，Hot+Warm 可以湊出 10 條結果（達到 K=10），
但第 6-10 條只是勉強相關。
Cold 裡有 1 條非常精準的記憶，但因為 Hot+Warm 已經湊滿 10 條，
fill-gap 不會觸發 → 這條記憶永遠不會被 promote。
```

### 這個 scenario 合理嗎？機率多高？

**非常合理，且發生機率高。** 這是 tiered top-K 系統的結構性缺陷，不是邊際案例。

**為什麼機率高：**

| 因素 | 影響 |
|------|------|
| Warm 有 5,000 條 | 是 Hot 的 10 倍，keyword/FTS5 overlap 的 surface 極大 |
| K=10 名額 | Warm 只需湊出 10 條「勉強相關」的結果就能卡位 |
| Cold 的定義 = 近期未被存取 | 這是自我強化的惡性循環：沒被存取 → 在 Cold → 更沒機會被存取 |
| 3-path RRF 的 Path K | 只要 Warm 記憶與 query 共享一個 keyword，就能拿到 RRF 分數 |
| Cold 記憶用不同詞彙 | 精準記憶常使用特定術語，與 query 的 general wording 不匹配 |

**具體計算：**

假設一個 query 有 3-5 個 keywords。Warm 有 5,000 條記憶，每條平均有 4 個 keywords。
這表示 Warm keyword space 約 20,000 個 keyword-memory pairs。

在一組 3-5 個 query keywords 中，Warm 的 20,000 pairs 裡至少命中 10 條的機率接近 **100%**。

結論：**這不是「如果」會發生的問題，而是「何時」會發生的問題。**

### 這不是馬太效應（Matthew Effect）嗎？

**確實是。** Tiered storage 如果只靠 fill-gap fallback，就是一個自強化的馬太效應系統：

```
存取的記憶 → 留在 Warm → 更容易被存取
未存取的記憶 → 掉入 Cold → 更難被存取
```

Fill-gap fallback 只在「Warm 不足 K 筆」時才檢索 Cold。但現實中 Warm 幾乎總是能湊滿 K 筆
（因為它有 5,000 條的 keyword space），所以 Cold 永遠不會被觸及。

---

## 2. Periodic Promotion Sampling 必要嗎？

### 我的判定：✅ 必要

**理由：沒有 Periodic Promotion Sampling，tiered storage 有可證明的正確性缺陷（proven correctness gap）。**

Formal 描述：
```
給定 query Q，存在記憶 M ∈ Cold，使得 relevance(M, Q) > relevance(M', Q) 
對某些 M' ∈ (Hot ∪ Warm)，但 fill-gap 永不觸發，M 永不 promote。
```

這個 gap 不是邊際案例，而是系統性問題。任何 tiered top-K 系統若沒有 Cold sampling，
**都會**有被埋沒的記憶。

### 但這需要多少 overhead？

關鍵問題不是「要不要做」，而是「最簡單的作法是什麼」。以下分析不需要 cron 的四種方案。

---

## 3. 四種實作方式評估

### Option A: Lazy Random Sampling on Query ✅ 推薦（給分：9/10）

**原理**：每 N 次 query，隨機抽 1-2 條 Cold 記憶，比對 keywords 與近期 query keywords。

```
每 10 次 query → random() < 0.1 觸發
  → SELECT COUNT(*) FROM memories WHERE tag = 'cold' → 隨機選 1-2 條
  → 對每條抽到的記憶：
    - 讀取其 keywords
    - 計算 keywords ∩ recent_query_keywords
    - 若有交集 → promote (update tag = 'warm')
```

**優點**：
- **極低 latency**：隨機 row lookup 是 O(1)，<1ms
- **自我限速**：每 10 次 query 才做一次，不會疊加
- **無背景 process**：完全 inline
- **自然衰退**：不常被 query 的 topics 不會被抽到

**缺點**：
- 隨機抽樣效率不高（可能抽到完全無關的 Cold 記憶）
- 極大 dataset（>100K）時，random row 可能不完全是 O(1)

**改良版本（A'）— 推薦實際使用**：

不純隨機，而是 **weighted by query keyword overlap**：

```
每 10 次 query 觸發
  → 維護 rolling query keywords（近 5 次 query 的 keywords 聯集）
  → SELECT memory_id, keyword FROM keywords WHERE memory_id IN (
      SELECT id FROM memories WHERE tag = 'cold' LIMIT 20  -- 先抽 20 個 candidate
    )
  → 對 20 個 candidate 計算 keywords ∩ rolling_query_keywords
  → 挑 overlap 最高的 1-2 條 promote
```

成本：~2 次 SQL query（random select + keyword join），分攤後 <2ms。

### Option B: Write-Time Promotion Check ✅ 推薦（給分：7/10，作為 A 的補充）

**原理**：每次寫入新記憶時，掃描 Cold tier 是否有 keywords 相似的舊記憶，一起 promote。

**優點**：
- Query latency 零影響（只在 store() 路徑上做事）
- 自然的語義聚類：新資訊會帶出相關舊資訊

**缺點**：
- **資料集靜止時無效**：如果使用者一個月不寫入新記憶，Cold 就永遠不會被採樣
- 初始部署時 burst：第一次 store() 可能要掃整個 Cold tier
- 不能獨立解決問題

**建議**：作為 Option A 的補充，雙管齊下。

### Option C: Startup Scan ❌ 不推薦（給分：2/10）

**原理**：Hermes 載入 MCP server 時掃一次 Cold tier。

**問題**：
- recall 是 lib（library），不是 service → 沒有固定的 startup event
- 載入 plugin 時 scan 不恰當（plugin init 應該快速）
- 對長期運行的 agent 沒有幫助（startup 只做一次）

### Option D: Promotion Threshold Decay ❌ 不解決問題（給分：3/10）

**原理**：降低 Cold 被 promote 的門檻（Cold 記憶在 fill-gap 中被 match 到一次，之後更容易 promote）。

**致命缺點**：**不解決命題本身。** 使用者的 scenario 是 Cold 記憶**從未出現在 fill-gap 中**，
因為 Warm 已經湊滿 K 筆。threshold decay 只對「偶爾能在 fill-gap 中出現的 Cold 記憶」有用，
對「被完全擠掉的 Cold 記憶」完全無效。

### 綜合建議

```
主要機制：Option A'（query-keyword-weighted lazy sampling）
輔助機制：Option B（write-time promotion check）
不需要：  Option C、Option D
```

---

## 4. Thrashing 風險分析

### 場景：Cold↔Warm 頻繁跳動

```
1. Cold 記憶 M 被 lazy sampling promote → Warm
2. 下一次 compaction：M 的 access_count 仍低 → 被 demote 回 Cold
3. 再下一次 lazy sampling：M 又被 promote → Warm
4. 重複 2-3 → M 在 Cold↔Warm 之間震盪
```

### 三個 guardrails 完全防止 thrashing

| Guardrail | 防止什麼 | 實作 |
|-----------|---------|------|
| **1. Observation window** | 短週期震盪 | Promote 後，強制在 Warm 停留 ≥24h（不因 access_count 低而 demote） |
| **2. Cooldown period** | 中週期震盪 | Demote 後，≥48h 內不能再次 promote |
| **3. Promotion cap** | 無限震盪 | 每條記憶最多 promote 3 次，之後永久 exempt from sampling |

**組合效果**：

```
Guardrail 1：防止「同一天內 promote/demote/promote」
Guardrail 2：防止「隔天 compaction 後又被 promote」
Guardrail 3：防止「第三輪以上的循環」

三者疊加 → Cold↔Warm thrashing 的機率降至 ~0%
```

### Score delta gate（額外安全措施）

只在以下條件 promote：
```
simulate_score(Cold_memory, rolling_query_keywords) > 0.7 × avg(Warm_top10_scores)
```

這確保只有「真正高品質」的 Cold 記憶被 promote，而非「勉強相關」的。這也抑制了 thrashing，
因為真正高品質的記憶在被 promote 後，自然會被 query 存取，提高 access_count，
從而不會在 compaction 中被 demote。

---

## 5. 最終投票

### 我是 Feynman

站在費曼的立場：**「你不能欺騙自己——而你正是最容易欺騙自己的人。」**

這裡最容易自欺的是：「Fill-gap fallback 已經夠了，Cold 不需要主動採樣。」
事實上，一個正確性有結構性缺陷的 tiered system，比沒有 tiered system 更危險——
因為它給你安全的錯覺。

### Q1: Periodic Promotion Sampling 必要還是不必要？

**✅ 必要。** 沒有它，tiered storage 保證會遺失正確的候選記憶。這不是效能最佳化，
而是正確性修復。

### Q2: 推薦哪個實作方式？

**Option A'（query-keyword-weighted lazy sampling）+ Option B（write-time check）作為補充。**

實作估算（以 recall 當前的 Python codebase）：

```
# Option A' 的 P0 實作：~30 行
# 不依賴任何新 dependency
# 只需要：
#   1. 一個 rolling keyword counter（dict[str, int]）
#   2. 一個 sampling counter（int）
#   3. 一次 random SELECT + keyword JOIN
#   4. UPDATE tag = 'warm' for matched memories
```

### Q3: 一句話結論

> **Tiered storage 沒有 Cold promotion sampling，就像漏斗沒有底部——資料進得去，出不來。用 query-keyword-weighted lazy sampling，30 行 code，<2ms latency，關掉 correctness gap。**
