# recall. P2 — 真實場景測試

## 方法
模擬 5 個 coding agent 場景：Session A 學到使用者偏好 → Session B 驗證 agent 是否記得。

每次測試：
1. 在 Session A 建立記憶（模擬對話歷史）
2. 在 Session B 提出相關問題
3. 用 `retrieve_relevant()` 查記憶
4. 判斷：正確的記憶是否在前 3 名？

## 場景

| # | Session A（學到的事） | Session B（問的問題） | 應回傳的記憶 |
|---|----------------------|----------------------|-------------|
| 1 | 使用者說「用 docker-compose 不要 Dockerfile」 | 「怎麼部署？」 | docker-compose 偏好 |
| 2 | 使用者說「資料庫用 PostgreSQL + asyncpg」 | 「資料庫用什麼？」 | PostgreSQL + asyncpg |
| 3 | 使用者說「API 結構用 routers/services/models 三層」 | 「新 API 放哪？」 | FastAPI 專案結構 |
| 4 | 使用者說「PR 沒 type hints 就打回去」 | 「程式碼品質要求？」 | type hints 政策 |
| 5 | 使用者說「伺服器遷移到 ECS Fargate」 | 「基礎設施計畫？」 | ECS Fargate 決策 |

## 結果
| 場景 | 命中？ | 備註 |
|------|--------|------|
| 1 | — | |
| 2 | — | |
| 3 | — | |
| 4 | — | |
| 5 | — | |
