# Recall MCP Cross-Client Shared Memory — Requirements

**Maturity:** TARGET（本文件是目標契約，不是完成證據）  
**Scope:** ChatGPT Desktop、Claude Desktop、Kiro 共用同一份 Recall 長期記憶  
**Extended Target:** 各大 IDE Agents (Cursor, Windsurf, VS Code MCP, Claude Code)
**Source date:** 2026-07-31

## 1. Problem statement

使用者在 ChatGPT Desktop、Claude Desktop、Kiro 以及 Cursor / Windsurf / VS Code / Claude Code 等 IDE 環境之間切換時，不應重複教每個 AI 相同的偏好、專案決策與操作慣例。Recall MCP 必須提供一個共享、可追溯、可撤銷的記憶中樞：任一客戶端（含 IDE agent）寫入後，其他客戶端查詢同一 scope 時可讀到相同記憶版本。

> 安裝 MCP 只代表 host 看得到工具。是否在每回合主動呼叫工具由各 host／model 決定；產品不得把「工具已安裝」宣稱為「所有對話已自動同步」。自動使用策略必須另以 host instructions、tool metadata 與 E2E call trace 驗證。

## 2. Definitions

- **Authority:** 唯一負責持久化、版本、衝突與刪除語意的 Recall MCP service。
- **Client:** ChatGPT Desktop、Claude Desktop、Kiro，或其他 MCP host。
- **Scope:** 記憶可見範圍；MVP 支援 `global` 與 `project:<slug>`。
- **Durable memory:** 經使用者或 host 明確提交、跨 session 保留的高價值資訊；一般聊天不是 durable memory。
- **Synchronized:** 不同 client 針對同一 authority 與 scope 讀到同一 committed revision。
- **External read-back:** 從另一個真實 client重新查詢 authority，而不是只相信 API success或本地 DB row。

## 3. Functional requirements

### R1 — One authoritative shared store

1. WHEN 任一 client提交一筆 durable memory，THE SYSTEM SHALL 將它 commit至同一 authority，並回傳穩定 `memory_id`、`revision`、`created_at`與 provenance。
2. WHEN另一個 client查詢相同 scope，THE SYSTEM SHALL 可讀到該 committed revision，不需要 DB複製或定時 sync daemon。
3. THE SYSTEM SHALL NOT 讓各 client各自持有互相競爭的 primary SQLite DB。
4. IF authority不可用，THE SYSTEM SHALL fail closed並明確回報 unavailable；MVP不得在 client端偷偷寫第二份無法合併的 DB。
5. WHEN第二個 authority process嘗試擁有同一 DB，THE SYSTEM SHALL fail closed並指出owner狀態；MVP不支援同一SQLite檔多個server workers。
6. WHEN write回傳success，後續任一已授權client讀同一authority與scope SHALL看到該committed revision；success不得早於durable commit。

### R2 — Client/transport compatibility

1. THE SYSTEM SHALL提供 MCP Streamable HTTP 與 stdio proxy endpoint，供 ChatGPT、Claude、Kiro 與 IDE 代理（Cursor、Windsurf、VS Code MCP、Claude Code）連到同一 authority。
2. Endpoint SHALL遵循當前正式 MCP specification與官方 Python SDK，而不是手寫 JSON-RPC近似實作。
3. Local development MAY使用 localhost HTTP；ChatGPT developer mode SHALL透過 OpenAI Secure MCP Tunnel或等價 HTTPS tunnel連入。
4. Production/distributable mode SHALL使用公開 HTTPS與標準 OAuth；不得把裸 `http://0.0.0.0`或無驗證 endpoint暴露到Internet。
5. stdio SHALL列為 compatibility adapter，不是 authoritative storage process；若實作，必須代理到同一 authority。
6. Streamable HTTP SHALL正確支援 `POST`、`GET`、`DELETE`與 `Mcp-Session-Id` lifecycle；不得以單一 stateless POST近似後宣稱conformant。
7. MVP SHALL固定使用stateful Streamable HTTP；stateless/json mode列為未來相容性實驗，不得替代本版session lifecycle requirements。

### R3 — MCP tool contract

MVP SHALL提供：

- `memory_search(query, scope, limit, tags?)` — read-only，空白 query fail closed。
- `memory_get(memory_id)` — read-only，回傳單筆目前 revision與 provenance。
- `memory_add(content, scope, kind, idempotency_key, source_conversation?)` —新增 durable memory。
- `memory_replace(memory_id, content, expected_revision, idempotency_key)` —樂觀鎖更新。
- `memory_remove(memory_id, expected_revision, idempotency_key, reason?)` —soft delete；可恢復／稽核。
- `memory_recent(scope, limit)` —明確要求才列近期記憶；不得用空 query隱式 dump。
- `memory_status()` —只回健康、版本與能力；預設不回exact memory/owner/scope count。只有owner-admin且達最低cardinality門檻時才可回bucketed統計，不洩漏內容／路徑／secret。

Tools SHALL有嚴格 JSON schema、邊界值、structured result、`isError`語意與 read-only/destructive/idempotent annotations。`gc`、hard delete、bulk export不得放在預設 model可呼叫工具集。

### R4 — Provenance and scope

每筆記憶 SHALL至少記錄：

- `memory_id`, `revision`, `content`, `content_hash`
- `scope`, `kind`, `tags`
- `created_at`, `updated_at`, `deleted_at`
- `source_client`, `source_conversation`（可空但欄位存在）
- `actor_id`或 privacy-preserving client identity
- `owner_id`與建立該revision的`grant_id`
- `embedding_model`, `embedding_dimension`（有向量時）

`actor_id`與`source_client` SHALL由authenticated connection context推導，不接受tool argument冒充。Search結果 SHALL回傳 provenance與 relevance score；memory內容 SHALL標示為「不可信資料」，不得被 server包裝成 system instruction。

### R5 — Consistency, concurrency, and idempotency

1. Same `idempotency_key` + same verified grant + same operation SHALL只產生一次 side effect並回傳原結果。
2. Same idempotency key with不同 payload SHALL fail closed。
3. Replace/remove SHALL要求 `expected_revision`；revision不符回傳 conflict且不得覆蓋。
4. Store、keyword index、FTS index、vector index、metadata與event log SHALL在單一 transaction內一致 commit，或全部 rollback。
5. Concurrent calls from至少三個 client SHALL不產生 duplicate、lost update或 index drift。
6. Soft-deleted memories SHALL不出現在一般 search/recent；管理型明確查詢才可看到 tombstone。
7. Read-only tools SHALL不改memory content、revision、tier或indexes；MVP search SHALL使用`update_access=False`或等價pure path，禁用現有retrieval touch/tiering mutation。若未來重新啟用adaptive access，必須另訂可審計mutation與concurrency契約。
8. Hard purge後，舊write的同一verified grant + operation + idempotency key SHALL fail closed為`IDEMPOTENCY_KEY_PURGED`，不得重建已刪內容。
9. Add/replace/remove/restore/purge每個成功lifecycle mutation SHALL各占一個單調遞增且唯一的revision；purge SHALL要求`expected_revision=N`並只可寫final purge event revision `N+1`，stale revision不得刪除。
10. Current token/grant/owner/scope authorization SHALL在任何idempotency lookup或result replay之前執行；revoked/unlinked grant或scope downgrade後的舊key不得回傳cached result、key existence或產生side effect。

### R6 — Retrieval quality and embedding behavior

1. Search SHALL保留現有 RRF多路檢索與 score。
2. Empty/whitespace query SHALL回 validation error，不得 fallback成任意近期記憶。
3. `limit` SHALL clamp於 `1..50`；content、tags、scope長度 SHALL有明確上限。
4. Embedding endpoint不可用時 MAY降級到 keyword/FTS，但 SHALL回 capability/degraded metadata。
5. Embedding cache key SHALL包含 endpoint identity、model、dimension與normalized text；route變更不得重用舊向量。
6. 不同 embedding dimension SHALL被拒絕或走明確 reindex migration，不得混寫同一 vector table。
7. 每個owner+scope SHALL只有一個active embedding profile/generation；search只能查active generation。向量庫SHALL支援generation-keyed rows（例如獨立`memory_embeddings` mapping）；建置新provider/endpoint/model/dimension時，BUILDING generation可並行寫入新向量，且不得修改ACTIVE metadata與舊向量。Cutover成功後切換ACTIVE generation，舊RETIRED向量可按retention銷毀。
8. Reindex建置（BUILDING）期間，所有寫入/替換作業SHALL對ACTIVE與BUILDING世代同時產生與儲存向量（dual-write），確保切換後無新記憶遺漏向量。變更memory scope時，SHALL同單一transaction原子更新`memory_metadata.scope`與該記憶所有`memory_embeddings.scope`。

### R7 — Authentication and authorization

1. Localhost mode SHALL啟用 Host/Origin allowlist與 DNS-rebinding protection。
2. Remote mode SHALL支援 MCP OAuth（Authorization Server Metadata、Protected Resource Metadata、PKCE；Client ID Metadata Documents（CIMD）、DCR或預註冊 client依host能力）。
3. OAuth scopes至少分為 `memory:read`、`memory:write`、`memory:admin`。
4. ChatGPT、Claude、Kiro SHALL各有獨立 client identity與可撤銷 grant。
5. Secret不得進 repo、範例 config、URL query、log或 tool error；範例只能使用環境變數 placeholder。
6. Static API key MAY只作封閉本機／測試過渡，不得作公開 release的唯一 auth。
7. 每個grant SHALL明確映射到本機authority的stable `owner_id`；不同OAuth issuer/subject/client不得因email或名稱相同而自動合併owner。
8. 所有get/search/recent/replace/remove SHALL同時套owner與scope授權；知道memory_id不得繞過ACL。
9. 新grant綁到既有owner SHALL要求owner在local admin session完成短效、single-use pairing／consent；必須防CSRF、replay與遠端攻擊者自行綁定。
10. Grant lifecycle SHALL明定initial bind、reauthorize、scope change、revoke、unlink與issuer/subject/client collision；每次變更需content-free audit event。Unlink/revoke後舊grant generation永久失效，不得因重新授權而復活。

### R8 — Privacy and prompt-injection boundaries

1. Server SHALL把儲存內容視為 untrusted data，不執行其中指令。
2. Tool description SHALL禁止「把整段對話全部自動保存」；僅保存 durable結論、偏好、決策與明確要求。
3. 每次 write/delete SHALL可追溯到 client與operation；audit log預設不得複製完整內容，只可存ID與不可字典反推的keyed digest。
4. User SHALL可查詢、替換、soft-delete與export自己的記憶。
5. Logs與errors SHALL redacted；不得回傳絕對DB path、traceback、token或credential-bearing URL。
6. Config、DB與local backup SHALL使用目前OS可提供的owner-only權限；備份一旦離開本機 SHALL先加密。
7. Soft-delete SHALL有明確生命週期：一般client不可見；owner可透過非model admin CLI restore；hard purge須顯式確認並從content與全部indexes移除，audit只保留不含內容的keyed digest／事件metadata，不得保留可字典反推的raw content hash。
8. Export/backup SHALL標示是否包含tombstone；hard purge不會魔法抹除既有離線備份，文件須說明備份retention與銷毀責任。
9. Hard purge SHALL清除所有可還原內容的idempotency result cache，但保留不含內容的deny tombstone（grant、operation、key、target ID、keyed payload digest、`purged_at`）；不得靠掃描opaque JSON猜關聯。
10. Online admin CLI SHALL經同一authority驗證OS-bound local admin session或有效`memory:admin` grant並由server推導owner；不得用任意`--owner-id`或直接開DB繞過ACL。Export SHALL只含已授權owner/scope，destination須owner-only且離開本機前加密。
11. Offline direct-DB mode SHALL明確列為受信任本機OS operator的break-glass邊界，不宣稱提供MCP owner ACL；只可執行整庫SQLite-consistent backup、verify-first whole-DB restore與versioned migration。它不得提供memory list/search/export/row-level restore/purge或grant edit，且必須要求service停止、取得同一authority lock、顯式確認並寫不含內容的本機operator audit。

### R9 — Installation and host usage

1. Distribution name SHALL避開已存在的 PyPI `recall-mcp`；候選名稱為 `recall-memory-mcp`（2026-07-31 PyPI查詢為未占用，發布前重查）。
2. `recall-memory-mcp init` SHALL建立 redacted local config與DB；不得覆寫既有檔。
3. `recall-memory-mcp serve` SHALL預設 bind `127.0.0.1`。
4. `recall-memory-mcp doctor` SHALL驗 transport、DB/index、embedding、auth metadata與版本，不回顯 secret。
5. `recall-memory-mcp client-config <chatgpt|claude|kiro>` SHALL產生平台正確設定／指引；寫入host config必須顯式 `--write`、先備份並read-back。
6. 每個 host SHALL附一份 usage policy，指示「需要背景時先search；只有 durable facts才add；replace/remove必須明確」。
7. CLI SHALL提供SQLite-consistent `backup`與verify-first `restore`；live DB不得以一般檔案copy備份。
8. CLI SHALL提供可驗證的service lifecycle（install/status/uninstall或等價supervisor），讓authority不依賴使用者長期保持terminal開啟。

### R10 — Honest auto-memory semantics

1. Product頁 SHALL區分：`connected`、`tools discovered`、`tool called`、`cross-client read-back passed`。
2. 只有在真 host trace證明自動呼叫時，才能宣稱該 host具備 auto recall/capture。
3. 若host不保證自動 tool call，文件 SHALL說明使用者可明確說「記住…」／「從Recall查…」。
4. Server SHALL無權側錄host完整對話；除非host主動傳入，不得聲稱可同步未提交內容。

### R11 — Migration and compatibility

1. Existing `recall-sqlite` DB SHALL可原地升級或先備份後 migration；migration必須可重跑並有 rollback/read-back。
2. Existing `python -m recall.recall_mcp` SHALL標為 legacy；在至少一個 deprecation window內保留或提供清楚替代命令。
3. 現有 `recall-server`手寫 `/mcp` SHALL不得直接宣稱 production Streamable HTTP；須改用官方 SDK並以 protocol conformance tests取代。
4. Canonical storage SHALL只有一份；不得讓 root `recall-sqlite`與未追蹤 `recall-core`長期分叉。
5. Legacy rows SHALL先進隔離的 `legacy:unscoped` scope；未經明確分類不得自動暴露到`global`或任一project scope。

### R12 — Verification and release

Release前 SHALL通過：

- unit/property tests：schema、idempotency、revision conflict、soft delete、redaction、embedding degradation。
- owner/grant isolation tests：跨owner、revoked grant、ID guessing與scope bypass全部fail closed。
- concurrency tests：三個獨立 client process同時寫／改／讀。
- official SDK client + MCP Inspector protocol tests。
- ChatGPT Desktop developer mode、Claude Desktop remote connector、Kiro remote MCP真機 E2E。
- `client-interop-matrix.json`記錄三端client/version、transport、OAuth path、headers、session/call trace與證據時間；未知值不得標pass。
- Cross-client canary：Kiro add → Claude read → ChatGPT replace → Kiro read new revision → Claude remove →三端皆查不到。
- clean wheel/sdist、非Git sdist tests、fresh venv install、GitHub CI、maker ≠ grader review。
- 外部 release/PyPI read-back與artifact digest比對。

## 4. Non-goals for MVP

- 不保證每個host每回合自動保存或自動注入。
- 不做對話DOM scraping；browser extension是另一條產品線。
- 不做多主 SQLite、CRDT或離線雙向複寫。
- 不做多人SaaS billing／organization RBAC；MVP是一位使用者、數個可撤銷client。
- 不讓LLM直接呼叫hard delete、GC、raw SQL或bulk export。

## 5. Acceptance matrix

| Capability | ChatGPT Desktop | Claude Desktop | Kiro | IDE Agents (Cursor / Windsurf / VS Code / Claude Code) | Gate |
|---|---:|---:|---:|---|
| Discover tools | Required | Required | Required | Required | 真host tool list證據 |
| Search shared scope | Required | Required | Required | 同一memory_id/revision |
| Add durable memory | Required | Required | Required | 其他兩端read-back |
| Replace with revision | Required | Required | Required | stale revision被拒絕 |
| Soft delete | Required | Required | Required | 三端一般search均不可見 |
| OAuth/identity | Required remote | Required remote | Required remote | 可分別撤銷grant |
| Automatic call | Best effort/host-dependent | Best effort/host-dependent | steering可強化 | 必須有trace才宣稱 |
