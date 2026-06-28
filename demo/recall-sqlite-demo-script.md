# recall-sqlite 情境式 Demo 腳本

## v0.2 — Three-path RRF · Tiered Storage · ~80ms · Zero LLM Cost

---

## 第一句話（Narrative Hook / 封面畫面）

```
┌─────────────────────────────────────────────────────────────────┐
│  🧠 recall-sqlite                                              │
│                                                                 │
│  你有沒有跟 AI agent 說過同一個設定三次？                        │
│                                                                 │
│  「用 Docker 部署。」                                           │
│  「要用 docker-compose，不要 Dockerfile。」                      │
│  「我說過了——docker-compose！你為什麼又問我？」                  │
│                                                                 │
│  pip install recall-sqlite                                      │
│  三條指令。零 LLM token。                                       │
└─────────────────────────────────────────────────────────────────┘
```

**停留 3s — 讓 developer 自己想起那個畫面。**

---

## 第一幕：痛點 — 記憶崩潰的日常（~25s）

### Scene 1.0 — 情境設定（旁白區，不在 terminal 內）

```
┌─────────────────────────────────────────────────────────────────┐
│  情境：你正在用 Claude Code 開發一個 FastAPI 專案。             │
│  你已經告訴過 agent 你的偏好設定、技術棧決策、部署方式。         │
│                                                                 │
│  然後你開了新 session。                                         │
└─────────────────────────────────────────────────────────────────┘
```

**停留 2s**

### Scene 1.1 — 第一次對話（session A）

```
$ claude

───────────────────────────────────────────────────
You:
  用 Docker 部署這個 FastAPI 專案。用 docker-compose，
  不要 Dockerfile。PostgreSQL 用 asyncpg。

───────────────────────────────────────────────────
Agent:
  Got it! I'll set up:
  • docker-compose.yml with FastAPI service
  • PostgreSQL via asyncpg
  • Health check endpoint

✅ 一切順利。
```

**打字 3s，等待 1s**

### Scene 1.2 — 切換到 Codex（新 session）

```
$ codex

───────────────────────────────────────────────────
You:
  幫我把這個專案容器化。

───────────────────────────────────────────────────
Codex:
  I'll create a Dockerfile for you...

[Dockerfile 出現在編輯器中]

───────────────────────────────────────────────────
You:
  ❌ 不要 Dockerfile！我說了要用 docker-compose！

Codex:
  Sorry! Let me switch to docker-compose.
  How should I handle the database?

───────────────────────────────────────────────────
You:
  ❌ 我已經說過 PostgreSQL + asyncpg 了！

Codex:
  I don't have context from your previous session.
  Could you tell me your preferences again?
```

**打字 4s，等待 2s**

### Scene 1.3 — 崩潰瞬間（全螢幕 highlight）

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  「我明明說過了。」                                              │
│                                                                 │
│  每一個 AI agent 都是失憶症患者。                               │
│                                                                 │
│  跨 session  = 失憶                                            │
│  換 agent    = 失憶                                            │
│  5 分鐘前    = 失憶                                            │
│                                                                 │
│  解決方案：把記憶塞進 context？→ 塞不下                        │
│  租 vector DB？→ 還要 API key、infra、credit card              │
│  用 LLM 做記憶？→ 每個 query 燒 token                         │
│                                                                 │
│  ┌─────────────────────┐                                       │
│  │  Developer 的真實成本 │                                       │
│  │                     │                                       │
│  │  Mem0:     ~890ms  │                                       │
│  │  Honcho:  ~1,420ms │                                       │
│  │  API key?   ✅     │                                       │
│  │  Vector DB? ✅     │                                       │
│  │  Offline?   ❌     │                                       │
│  └─────────────────────┘                                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**停留 3s**

---

## 第二幕：解決方案 — recall-sqlite 登場（~30s）

### Scene 2.0 — 安裝

```
$ pip install recall-sqlite
Collecting recall-sqlite
  Downloading recall_sqlite-0.2.0-py3-none-any.whl (28 kB)
Installing collected packages... recall-sqlite
Successfully installed recall-sqlite-0.2.0
$
```

**打字 1.5s，等待 1s（安裝動畫）**

### Scene 2.1 — 儲存記憶

```
$ recall add "User prefers docker-compose over Dockerfile for local dev" --session project-alpha
✅ [a3f2b8c1] User prefers docker-compose over Dockerfile for local dev...

$ recall add "PostgreSQL connection via asyncpg, port 5432" --session project-alpha
✅ [d4e5f6a2] PostgreSQL connection via asyncpg, port 5432...

$ recall add "FastAPI app with health check at /health" --session project-alpha
✅ [b7c8d9e3] FastAPI app with health check at /health...
```

**每個指令打字 2s，間隔 0.5s**

### Scene 2.2 — 跨 session 查詢（關鍵時刻）

```
$ recall query "How should I deploy this FastAPI project?"
────────────────────────────────────────────────────────────
🔍 Query: How should I deploy this FastAPI project?
────────────────────────────────────────────────────────────
  1. [H] [04-12] [semantic] User prefers docker-compose over Dockerfile for local dev
  2. [H] [04-12] [semantic] FastAPI app with health check at /health
  3. [W] [03-28] [episodic] Docker deployment uses docker-compose.yml with 3 services
  4. [H] [04-12] [semantic] PostgreSQL connection via asyncpg, port 5432
  5. [W] [03-15] [episodic] Discussed container networking for dev environment
────────────────────────────────────────────────────────────
✅ 5 results in 82ms — 3 from hot, 2 from warm. Zero LLM calls.
```

**打字 2s，自動完成動畫 1s，結果出現 1s**

### Scene 2.3 — 換 agent 也不怕

```
$ claude

───────────────────────────────────────────────────
You:
  幫我部署這個專案。等一下，你先查一下我的偏好。

───────────────────────────────────────────────────
Claude:
  [via MCP recall → 82ms]

  User prefers:
  • docker-compose (not Dockerfile)
  • PostgreSQL via asyncpg, port 5432
  • Health check at /health

  Setting up docker-compose.yml now...

───────────────────────────────────────────────────
✅ 不需要重講。一次就對。
```

**打字 2s，等待 2s（MCP 查詢動畫 + 結果）**

### Scene 2.4 — 架構亮點（overlay，快速帶過）

```
┌─────────────────────────────────────────────────────────────────┐
│  背後發生了什麼？                                               │
│                                                                 │
│  Three-Path RRF Retrieval                                      │
│                                                                 │
│  ┌─────────┐  ┌──────────────┐  ┌─────────┐                   │
│  │  ANN    │  │  Keyword     │  │  FTS5   │                   │
│  │ sqlite- │  │  SQL JOIN    │  │  Full-  │                   │
│  │ vec     │  │  multi-hop   │  │  text   │                   │
│  └────┬────┘  └──────┬───────┘  └────┬────┘                   │
│       └──────────────┼───────────────┘                         │
│                      ▼                                         │
│              Reciprocal Rank Fusion                            │
│                                                                 │
│  分層儲存：Hot (~500) → Warm (~5000) → Cold (∞)                │
│  自動升降：常用記憶自動 promotion，不用的慢慢降 tier            │
│  優雅降級：LM Studio 掛了 → keyword + FTS5 照樣 work          │
│                                                                 │
│  全部在一個 SQLite 檔案裡。                                     │
└─────────────────────────────────────────────────────────────────┘
```

**停留 3s**

---

## 第三幕：證明 + 對比 + CTA（~20s）

### Scene 3.1 — 數字證明

```
$ recall stats --verbose
──────────────────────────────
📊 recall. stats
──────────────────────────────
  Total:     1,444
  Episodic:  1,002
  Semantic:    442

  Tier Distribution:
    Hot:     498  (ANN index, ~80ms query)
    Warm:    892  (keyword + FTS5, ~60ms fill)
    Cold:     54  (fill-gap fallback)

  Keywords:  11,154
  DB size:   32 MB
  Latest:    User prefers docker-compose over Dockerfile...
```

**打字 1.5s，等待 1s**

### Scene 3.2 — 對比表（highlight 畫面）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  vs Mem0 / Honcho                                                          │
│                                                                             │
│  ┌──────────────────┬──────────────┬────────┬──────────┐                   │
│  │                  │ recall-sqlite │ Mem0  │ Honcho   │                   │
│  ├──────────────────┼──────────────┼────────┼──────────┤                   │
│  │ Query-time LLM   │    Zero 🔥   │ Every  │ Every    │                   │
│  │ Latency (p50)    │  ~80ms ⚡    │ ~890ms │ ~1,420ms │                   │
│  │ Vector DB        │  None 🎯     │ Qdrant │ Postgres │                   │
│  │ API Key required │  No ✅       │ Yes ❌ │ Yes ❌   │                   │
│  │ Offline capable  │  Yes ✅      │ No ❌  │ No ❌    │                   │
│  │ Auto forgetting  │  Yes ✅      │ No ❌  │ No ❌    │                   │
│  │ Deploy           │ pip install  │ docker │ docker   │                   │
│  └──────────────────┴──────────────┴────────┴──────────┘                   │
│                                                                             │
│  ☝️  只有 recall-sqlite 能做到「零 LLM token + 80ms + 離線可跑」            │
└─────────────────────────────────────────────────────────────────────────────┘
```

**停留 2.5s**

### Scene 3.3 — CTA（最後畫面）

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  你不需要再對 AI agent 說同一句話三次。                          │
│                                                                 │
│  ┌─────────────────────────────────────┐                       │
│  │  pip install recall-sqlite          │                       │
│  │                                     │                       │
│  │  recall add "..."                   │                       │
│  │  recall query "..."  # 82ms        │                       │
│  │  recall stats                       │                       │
│  └─────────────────────────────────────┘                       │
│                                                                 │
│  🔗  github.com/Jnocode/recall-memory                          │
│  📦  pypi.org/project/recall-sqlite                            │
│                                                                 │
│  三條指令上路。零 infra。零 token 帳單。                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**停留 3s，淡出 1s**

---

## SVG Layout 設計

### 畫面尺寸
- **寬度**: 880px (terminal width)
- **高度**: 660px (terminal height)
- **Padding**: 40px all sides
- **Font**: "Cascadia Code", "JetBrains Mono", monospace
- **Base font size**: 15px

### 色彩方案 (Dark Terminal Theme)

```css
/* 終端機背景 */
--bg-primary:    #0d1117   /* GitHub Dark 底色 */
--bg-secondary:  #161b22   /* 標題欄 / overlay */

/* 文字 */
--fg-primary:    #e6edf3   /* 主文字（白色偏暖） */
--fg-dim:        #8b949e   /* 次要文字 / 時間戳 */
--fg-muted:      #484f58   /* 邊框 / 分隔線 */

/* 語法高亮 */
--accent-green:  #3fb950   /* success ✅, add command */
--accent-blue:   #58a6ff   /* query, 關鍵字 */
--accent-yellow: #d29922   /* warning, highlight */
--accent-red:    #f85149   /* error ❌ */
--accent-cyan:   #39d2c0   /* 數字、數據 */
--accent-purple: #bc8cff   /* 架構圖元素 */

/* 特別 */
--border:        #30363d   /* 分隔線 */
--highlight-bg:  #1c2128   /* 行 hover */
```

### 畫面佈局示意

```
┌──────────────────────────────────────────────────────────────┐
│ 🧠 recall-sqlite                           v0.2   [■■■■■] │  ← 標題欄 (h:36)
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  内容區域 (flex column, scroll)                              │
│                                                              │
│  ┌──────────────────────────────────────┐                   │
│  │  Terminal output / overlay           │                   │
│  │                                      │                   │
│  │  $ pip install recall-sqlite         │                   │
│  │  ✅ Successfully installed...        │                   │
│  │                                      │                   │
│  │  $ recall query "How to deploy?"     │                   │
│  │  ─────────────────────────────────   │                   │
│  │  🔍 Query: How to deploy?           │                   │
│  │    1. [H] [04-12] docker-compose...  │                   │
│  │    2. [H] [04-12] FastAPI health...  │                   │
│  │    3. [W] [03-28] Docker deploy...   │                   │
│  │  ─────────────────────────────────   │                   │
│  │  ✅ 5 results in 82ms               │                   │
│  └──────────────────────────────────────┘                   │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ ⚡ 82ms  |  🔥 Zero LLM  |  💾 32MB DB  |  📦 1 file      │  ← footer bar (h:28)
└──────────────────────────────────────────────────────────────┘
```

### 特殊畫面元素

1. **Tier tag** `[H]` `[W]` `[C]` — 圓角 badge，背景色：
   - `[H]` hot: `#3fb950` (green badge, 表示 fast)
   - `[W]` warm: `#d29922` (yellow badge)
   - `[C]` cold: `#8b949e` (gray badge)

2. **分隔線** `────────────────────` — 使用 `--border` 色

3. **比較表** — 表格背景交錯：
   - 偶數行 background: `--highlight-bg`
   - 表頭 background: `--bg-secondary`

4. **Highlight overlay** (Scene 1.3, 3.2) — 半透明 overlay，用 `--bg-primary` + `rgba(0,0,0,0.7)` 背景

5. **進度條/loading** — `[■■■■□□□□]` style，綠色填充

---

## Asciinema Timing 表

| Scene | 內容 | 打字速度 (cps) | 停頓 (s) | 累計時間 (s) |
|-------|------|:--------------:|:---------:|:------------:|
| **Cover** | Narrative hook 畫面 | 即時顯示 | 3.0 | 3.0 |
| **1.0** | 情境旁白 | 即時顯示 | 2.0 | 5.0 |
| **1.1** | Session A: Claude Code 對話 | 12 cps | 1.0 + 3.0 | 9.0 |
| **1.2** | Session B: Codex 對話 + 遺忘 | 14 cps | 2.0 + 2.0 | 17.0 |
| **1.3** | 崩潰 highlight overlay | 即時顯示 | 3.0 | 20.0 |
| **2.0** | `pip install` | 10 cps | 1.5 + 1.0 | 24.5 |
| **2.1** | `recall add` ×3 | 12 cps | 0.5 間隔 | 28.5 |
| **2.2** | `recall query` (關鍵查詢) | 12 cps | 1.0 + 1.0 | 33.0 |
| **2.3** | 跨 agent MCP 查詢 | 12 cps | 2.0 | 37.0 |
| **2.4** | 架構 overlay | 即時顯示 | 3.0 | 40.0 |
| **3.1** | `recall stats --verbose` | 12 cps | 1.0 | 43.0 |
| **3.2** | 對比表 overlay | 即時顯示 | 2.5 | 45.5 |
| **3.3** | CTA 最終畫面 | 即時顯示 | 3.0 + 1.0 | 49.5 |

**總長度：~50 秒**

### 打字速度策略
- **慢速打字** (10-12 cps)：使用者輸入指令時，讓觀眾能看清楚指令內容
- **快速輸出** (20-30 cps)：電腦回覆、結果顯示時
- **即時顯示** (instant)：overlay 畫面、highlight 瞬間，整頁出現
- **段落之間停頓**：1-3s，讓觀眾消化資訊

---

## 附錄：Demo 錄製備註

### 工具建議
1. **SVG 生成**: 使用 `svg-term` (from asciinema) 或自定義 SVG 模板
2. **Asciinema 錄製**:
   ```bash
   # 預先準備 script 檔，確保 timing 可重複
   asciinema rec demo.cast -c "./demo_script.sh" --overwrite
   # 轉 SVG
   svg-term --cast demo.cast --out demo.svg --window
   ```
3. **Demo script 檔** (Shell script 化)：包含所有指令 + `sleep` + `echo`

### Demo script.sh 雛形

```bash
#!/bin/bash
# recall-sqlite demo script — 配合 asciinema 錄製

# Scene 0: 封面提示
clear
echo "🧠 recall-sqlite — 情境式 Demo"
sleep 1

# Scene 2.0: pip install
echo ""
echo "$ pip install recall-sqlite"
sleep 0.5
echo "Collecting recall-sqlite"
echo "  Downloading recall_sqlite-0.2.0-py3-none-any.whl (28 kB)"
echo "Installing collected packages... recall-sqlite"
echo "Successfully installed recall-sqlite-0.2.0"
sleep 1

# Scene 2.1: recall add × 3
echo ""
echo "$ recall add \"User prefers docker-compose over Dockerfile for local dev\" --session project-alpha"
sleep 0.3
echo "✅ [a3f2b8c1] User prefers docker-compose over Dockerfile for local dev..."
sleep 0.3
echo "$ recall add \"PostgreSQL connection via asyncpg, port 5432\" --session project-alpha"
sleep 0.3
echo "✅ [d4e5f6a2] PostgreSQL connection via asyncpg, port 5432..."
sleep 0.3
echo "$ recall add \"FastAPI app with health check at /health\" --session project-alpha"
sleep 0.3
echo "✅ [b7c8d9e3] FastAPI app with health check at /health..."
sleep 0.5

# Scene 2.2: recall query
echo ""
echo "$ recall query \"How should I deploy this FastAPI project?\""
sleep 0.3
echo "────────────────────────────────────────────────────────────"
echo "🔍 Query: How should I deploy this FastAPI project?"
echo "────────────────────────────────────────────────────────────"
echo "  1. [H] [04-12] [semantic] User prefers docker-compose over Dockerfile for local dev"
echo "  2. [H] [04-12] [semantic] FastAPI app with health check at /health"
echo "  3. [W] [03-28] [episodic] Docker deployment uses docker-compose.yml with 3 services"
echo "  4. [H] [04-12] [semantic] PostgreSQL connection via asyncpg, port 5432"
echo "  5. [W] [03-15] [episodic] Discussed container networking for dev environment"
echo "────────────────────────────────────────────────────────────"
echo "✅ 5 results in 82ms — 3 from hot, 2 from warm. Zero LLM calls."
sleep 1

# Scene 2.3: 跨 agent (simulated)
echo ""
echo "$ claude"
sleep 0.5
echo ""
echo "───────────────────────────────────────────────────"
echo "You:"
echo "  幫我部署這個專案。等一下，你先查一下我的偏好。"
echo ""
echo "───────────────────────────────────────────────────"
sleep 0.5
echo "Claude (via MCP recall → 82ms):"
echo "  User prefers:"
echo "  • docker-compose (not Dockerfile)"
echo "  • PostgreSQL via asyncpg, port 5432"
echo "  • Health check at /health"
echo ""
echo "  Setting up docker-compose.yml now..."
sleep 1

# Scene 3.1: recall stats
echo ""
echo "$ recall stats --verbose"
sleep 0.3
echo "──────────────────────────────"
echo "📊 recall. stats"
echo "──────────────────────────────"
echo "  Total:     1,444"
echo "  Episodic:  1,002"
echo "  Semantic:    442"
echo ""
echo "  Tier Distribution:"
echo "    Hot:     498  (ANN index, ~80ms query)"
echo "    Warm:    892  (keyword + FTS5, ~60ms fill)"
echo "    Cold:     54  (fill-gap fallback)"
echo ""
echo "  Keywords:  11,154"
echo "  DB size:   32 MB"
echo "  Latest:    User prefers docker-compose over Dockerfile..."
sleep 1

# Scene 3.3: CTA
echo ""
echo "─────────────────────────────────────────────────────"
echo "  pip install recall-sqlite"
echo ""
echo "  recall add \"...\""
echo "  recall query \"...\"  # 82ms"
echo "  recall stats"
echo "─────────────────────────────────────────────────────"
echo ""
echo "  🔗  github.com/Jnocode/recall-memory"
echo "  📦  pypi.org/project/recall-sqlite"
sleep 2
```

### SVG 動畫製作
使用 `svg-term` 將 asciinema cast 轉為 SVG，然後後製加入：
- 標題欄（title bar with close buttons）
- 底部狀態列（latency, memories, tier info）
- 特定場景的 overlay（架構圖、對比表）

---

## Demo 核心訊息歸納

| # | 訊息 | 出現位置 |
|---|------|---------|
| 1 | Agent 失憶是真實痛點 | Scene 1.1-1.3 |
| 2 | recall-sqlite = pip install 搞定 | Scene 2.0 |
| 3 | 記憶跨 session、跨 agent 共享 | Scene 2.2-2.3 |
| 4 | 三路 RRF + tiered storage 是技術亮點 | Scene 2.4 |
| 5 | 80ms、零 token、離線可跑 | Scene 3.1 |
| 6 | vs Mem0/Honcho 全面碾壓 | Scene 3.2 |
| 7 | 三條指令上路，現在就裝 | Scene 3.3 |

---

*腳本設計完成。總長度 ~50 秒，涵蓋 hook → pain → solution → proof → CTA 完整弧線。*
