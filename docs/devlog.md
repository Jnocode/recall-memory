# recall. 🧠 開發日誌

> 記錄 recall-memory 開發過程中的決策、踩坑與解法。
> 目標：取代 Honcho 成為 Hermes 社群的記憶解決方案。

---

## 2026-06-23：記憶系統整合方案三審通過

### 決定
三審（費曼、Karpathy、Musk）通過記憶系統整合方案 v3：
- **recall = 目的地**（SAG 論文實作，改善到能取代 Honcho）
- **Honcho = 橋樑**（階段性任務，解鎖現有 1625 條 embeddings）
- **Plugin 發布**：`recall-memory-hermes` Hermes plugin，不改 core

### 完成項目
- 5 條 memory tool facts 遷入 recall（1400 → 1405 memories）
- Honcho vs recall gap analysis 完成
- Hermes provider plugin 確認為 Phase 2 P0

### 已知缺口
- Hermes provider plugin：recall 取代 Honcho 的管道
- Session/peer context model：recall 有 session_id 欄位但無結構化模型
- Collections/tag 強化
