# recall-sqlite Demo: Retention Architecture v1.0

> **目標：** Asciinema 錄製，90 秒內讓開發者決定「我要裝這個」  
> **角色：** Retention Architect（不管技術，只管人有沒有在看）  
> **工具：** recall-sqlite v0.2（SQLite + sqlite-vec + FTS5 hybrid retrieval）  
> **格式：** 情境式 demo（非教學、非 walkthrough）

---

## 一、Hook 公式（前三秒定生死）

### 第一眼畫面

```
❯ agent: "deploy the docker thing"
❯ recall ◇ 找到 3 條相關記憶（來自 23 天前）
    [episodic] user prefers docker-compose over Dockerfile
    [episodic] last deployment used docker-compose --build
    [semantic] project docker-compose template at ./infra/docker-compose.yml
```

**為什麼這樣開場：**
- 第一行是 agent 收到指令，不是使用者在裝東西 — 開發者會想「他怎麼知道的？」
- 「23 天前」是最強 hook 數字 — 秒殺「這東西有長期記憶」
- 三行結果各自不同來源（episodic / semantic），白話展示 hybrid retrieval
- 沒有任何 SQL 或 pip install — 不是 walkthrough，是情境

### Hook 確認清單

| 元素 | 實現方式 | 秒數 |
|------|---------|------|
| 視線第一落點 | 上方的 `recall ◇` icon + 橫線 | 0-0.5s |
| 「跟我有關」信號 | "user prefers docker-compose" — 任何開發者都用過 Docker | 0.5-2s |
| 「哇」數字 | **23 天前** — 打破「session 記憶只活幾小時」的預期 | 2-3s |
| 技術身分識別 | `found 3 related memories` + 向量/FTS/實體混搭暗示 | 3s |

---

## 二、Retention 結構（前 30 秒逐格設計）

### 時間軸總圖

```
sec  ─── 場景 ───          ─── 資訊密度 ───         ─── 視覺主體 ───
 0   █ hook: agent 接收指令   高（3 行結果）          大: 23天前 / recall ◇
 5   █ agent 自動回應         更高（punch 行）        小: agent 輸出的技術細節
10   █ 使用者追問新問題       更高（cross-session）   大: 新 query 字
15   █ recall 跨 session 匹配  punch（最舊記憶）      大: 日期差距（如有 >30 天）
20   █ surprise moment        最高（驚喜）            大字 flash: "75% less repetition"
25   █ CTA: 一行指令           低（留白）              大字: pip install recall-memory
30   █ end card / repo link   留白                    小: github.com/Jnocode/recall-memory
```

### 逐格腳本

#### 0s-5s：Hook（上面已設計）
**新資訊：** `recall ◇` icon、23 天前、三種記憶類型  
**不要告訴觀看者的事：** 這是 SQLite、這是 Python、需要裝什麼依賴

#### 5s-10s：Agent 自動用記憶回應
```
❯ agent: "deploy the docker thing"
❯ recall ◇ 找到 3 條相關記憶（來自 23 天前）
    [episodic] user prefers docker-compose over Dockerfile
    [episodic] last deployment used docker-compose --build
    [semantic] project docker-compose template at ./infra/docker-compose.yml

❯ assistant: 用 docker-compose 部署。
    docker-compose up -d --build
    ✅ 部署完成 (12.3s)
```
**新資訊：** agent 真的用了記憶來回應（不是秀資料而已）  
**視覺：** agent 輸出縮排，跟 recall 結果視覺分層

#### 10s-15s：使用者故意換話題測試記憶邊界
```
❯ user: 那上次那個資料庫 migration 是怎麼跑的？
❯ recall ◇ 找到 2 條相關記憶（來自 17 天前 + 6 天前）
    [episodic] ran alembic upgrade head on staging
    [episodic] migration failed due to foreign key constraint
```
**新資訊：** recall 能跨 session 追不同主題  
**關鍵：** 第一次 query 是 deploy，第二次是 migration — 完全不同主題，但 recall 都找到  
**觀看者心理：** 「他真的沒手動喂 context 嗎？」

#### 15s-20s：Punch — 顯示最舊記憶的震撼彈
```
❯ assistant: staging 跑過 alembic upgrade head，但 foreign key 卡住過。
    要加 --skip-fk-check 跳過檢查，或者在 production 先跑 dry-run。

❯ user: 等等，你怎麼知道那次的？那是三個禮拜前的事了。
❯ recall ◇ session lineage:
    session: deploy-docker-compose (今日)
    session: db-migration-staging (17 天前)
    session: alembic-setup (38 天前) ← hybrid recall 找到的底層記憶
```
**新資訊：** recall 不只找到關鍵字匹配，還透過 entity overlap 溯源到更早的 session  
**Punch 點：** 38 天前的記憶被自動找到 — 這不是 RAG，這是 persistent agent memory

#### 20s-25s：Surprise Moment（Asciinema 專屬）
```
❯ user: 那我的 redis config 偏好呢？我有講過嗎？
❯ recall ◇ 找到 1 條相關記憶
    [semantic] user prefers redis+sentinel over standalone (157 天前)
    ⚠️ 這個記憶的 embedding 跟 query 只有 0.31 相似度
    ⚠️ 但 entity overlap (redis) + FTS keyword 救回來了
```
**Surprise 設計原理：**
- 觀看者預期：「找不到吧，157 天太久了」
- 實際結果：找到了 — 而且 recall 誠實顯示 0.31 低相似度但還是找到了
- **「Wait, what?」時刻：** 157 天 / 0.31 similarity / 3-path RRF 的誠實展示
- 這不是 demo magic — 這是架構的誠實展現：hybrid retrieval 在純向量失敗時仍有救

**為什麼這在 asciinema 有效：**
- 觀看者無法暫停去查 source code（asciinema 特性）
- `0.31 similarity` 這種誠實數字讓觀看者相信這不是 scripted
- 「這是我自己跑也看不到的內部決策過程」— 不是「我也可以 terminal 打一樣的指令」

#### 25s-30s：CTA — 一行指令
```
❯ user: 所以這整套是怎麼裝的？
❯ 終端機畫面清乾淨

    pip install recall-memory
    # 124KB · 單一 SQLite 檔案 · 零常駐服務
    # 你的 agent 從此不會再問已經知道的事。
```
**資訊密度驟降（留白策略）：**
- 三行字，大量空白
- 前面 25 秒塞滿了資訊，這裡是呼吸點
- CTA 是安裝指令，不是社群連結、不是 star 數、不是 feature list

#### 30s+：End Card
```
     ┌─────────────────────────────────────────┐
     │  github.com/Jnocode/recall-memory       │
     │                                         │
     │  "Your agent stops asking what it       │
     │   already knows."                       │
     └─────────────────────────────────────────┘
```

---

## 三、視覺層級指令（Asciinema 錄製用）

### 字體大小 / 粗細對應

| 視覺元素 | 優先級 | Asciinema 實現方式 |
|----------|--------|-------------------|
| `recall ◇` icon | 最高 | 亮色（白/青）字首，其餘灰 |
| 日期數字（23 天前） | 最高 | 用 `\e[1m` bold |
| session lineage 時間軸 | 高 | 縮排 + dim color |
| query 字串 | 中 | default color |
| similarity 分數 | 中（surprise 時提高） | 黃色（`\e[33m`） |
| agent 輸出 | 低 | 綠色 dim（`\e[32;2m`） |
| error / warning | 驚喜用 | 黃底 `⚠️` 符號 |

### 顏色引導策略

```
藍/青色 (#00bcd4) → recall 的 brand color，只用在 recall 輸出的 prefix
綠色          → agent 執行動作（非對話）
白色          → user 輸入
黃色          → surprise / 內部決策揭露
灰色 dim      → 輔助資訊（session id、時間戳、技術細節）
```

### 螢幕空間配置

- **上半部（0-60%）：** 永遠是最近的對話 / recall 結果
- **下半部（60-100%）：** 漸層資訊（session lineage、similarity score、技術決策）
- **關鍵原則：** 新資訊永遠出現在上半部，下半部是當下的補充。觀看者不需要上下掃描才能理解當前發生什麼。

---

## 四、Asciinema 版特殊要求對照

| 要求 | 設計解法 |
|------|---------|
| 不要讓觀看者覺得「我也可以自己跑這些指令」 | 永遠不純秀 CLI command → 秀的是 **agent 的內部決策過程**（similarity score、entity match、session lineage tree）。這些不是 terminal command 能複製的。 |
| 「wait, what?」surprise moment | **0.31 similarity 但還是找到** — 觀看者無法預期這個結果，因為純向量系統在 0.31 不會 return。Hybrid RRF 的誠實展示就是 surprise 本身。 |
| 不要像教學 | 沒有 `# Step 1: Install`、沒有說明文字。所有資訊透過 terminal 輸出自然展現。 |
| 製造「這個人不知道自己在錄 demo」的真實感 | Recall 誠實顯示低分匹配、session lineage 交錯、偶爾找到預期外的舊記憶。不是「完美的 RAG 命中」。 |

---

## 五、意外發現的「bonus hook」設計

在 retention 結構的 15s 處，當 user 說「你怎麼知道那次的」，這實際上產生了**第二個 hook**：

```
user: 等等，你怎麼知道那次的？那是三個禮拜前的事了。
```

這段對話讓觀看者把自己代入 user 的角色 — **「對啊，我也想知道你怎麼知道的」**。  
這比任何技術解釋都更能留住人，因為它製造了**好奇心缺口（curiosity gap）**。

### 原則
- 不要讓 agent 在第一輪就完美回答所有問題  
- 讓 user 在 demo 中出現「被 recall 嚇到」的反應  
- 觀看者會跟 user 同步體驗驚訝 → 信任 → 想擁有

---

## 六、Retention 失敗防禦

### 觀看者在第 N 秒可能關掉的原因

| 秒數 | 關掉原因 | 防禦機制 |
|------|---------|---------|
| 3s | 「又是 RAG demo」 | 第一行就秀 cross-session + 23 天，不是 keyword search |
| 8s | 「太慢，等不及」 | 第二個 query 在 10s 就進來，節奏快 |
| 15s | 「這跟我的場景無關」 | Docker + migration → 任何 backend dev 都懂 |
| 22s | 「太完美，假的」 | Surprise 環節秀 0.31 similarity + 157 天 — 誠實瑕疵製造信任 |
| 28s | 「所以我要裝什麼」 | CTA 精準到位，不多一句廢話 |

### 資訊密度曲線（避免疲勞）

```
密度
高 ┤    ╱╲    surprise
   │   ╱  ╲    ╱╲
   │  ╱    ╲  ╱  ╲
   │ ╱      ╲╱    ╲
低 ┤╱              ╲______
   0  5  10  15  20  25  30 (秒)
```

**原則：** 不是線性遞增，而是波浪式 — 每 5 秒一個小峰，20s 主峰，25s 驟降讓觀看者呼吸。

---

## 七、實作檢查清單

- [ ] Asciinema 錄製前先 dry-run 兩次（第一次抓 timing，第二次 refine 視覺）
- [ ] `recall ◇` icon 用 unicode (U+25C7) + `\e[36m` cyan
- [ ] 日期數字統一用 `\e[1m` bold white
- [ ] `⚠️` 黃底驚嘆號只在 surprise moment 出現（不要稀釋）
- [ ] 確定 terminal 沒有 scrollbar（asciinema 全螢幕）
- [ ] 字型用 Fira Code 或 JetBrains Mono（等寬 + 連字）
- [ ] 不要顯示 prompt 路徑（`~/workspace/project$` 太多雜訊）→ 只用 `❯`
- [ ] 所有 session ID 用可讀名稱（`deploy-docker-compose`），不要用 uuid
- [ ] 錄製前關閉 terminal 的 title bar、tab bar、menu

---

## 八、一句話總結

> **前 3 秒讓開發者看到「agent 記得 23 天前的事」，  
> 前 30 秒讓他相信「這是 hybrid retrieval 不是 magic」，  
> 第 31 秒他已經在裝了。**
