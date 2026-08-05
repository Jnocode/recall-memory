# Recall MCP Cross-Client Shared Memory — Design

**Maturity:** TARGET with existing PARTIAL prototypes  
**Companion:** `requirements.md`, `tasks.md`  
**Source date:** 2026-07-31

## 1. Decision summary

建立一個**單一權威 Recall MCP service**。ChatGPT Desktop、Claude Desktop與Kiro透過同一 Streamable HTTP `/mcp` endpoint讀寫；本機開發由 localhost提供，ChatGPT developer mode透過 Secure MCP Tunnel／HTTPS tunnel進入，公開部署使用 HTTPS + OAuth。

不採「每個桌面host各自spawn一個會直接寫SQLite的MCP process」作主要架構。SQLite WAL雖可處理有限併發，但多 authority會讓idempotency、revision、embedding route、migration與audit難以維持一致。若後續需要 stdio，stdio只做連到同一 authority的 compatibility bridge。

## 2. Current-state claim ledger

| Claim | Artifact | Classification | Reason |
|---|---|---|---|
| Public stdio MCP exists | `src/recall/recall_mcp.py` | PARTIAL | 手寫 JSON-RPC；最後GitHub修改為2026-06-25；無MCP SDK conformance tests |
| HTTP `/mcp` exists | untracked `recall-server/src/recall_server/mcp.py` | PARTIAL | 能處理部分methods，但不是正式SDK session transport，且整個`recall-server/`未追蹤、無tests |
| Cross-client HTTP MCP complete | `ARCHITECTURE.md`, `docs/ai-carrier-integration.md` | CONTRADICTED | 文件標✅，但GitHub master沒有`recall-server/`，也沒有真host E2E evidence |
| Shared retrieval core | root `src/recall/*` | PARTIAL / source verified | 公開master有RRF、SQLite WAL、score/session filter；mixed local suite尚非green |
| Current mixed test suite | local `tests/` including untracked P0 tests | BLOCKED baseline | 2026-07-31以uv+pytest執行：21 passed, 1 skipped, 3 failed, 4 errors；untracked server tests引用未宣告的FastAPI dependency |
| Client auto memory after install | none | TARGET | MCP tool discovery不等於host自動呼叫 |
| Production auth | optional static API key prototype | PARTIAL | 不符合公開ChatGPT/Claude/Kiro OAuth產品契約 |

## 3. Authoritative sources checked

- OpenAI, **Building MCP servers for plugins and API integrations**, read 2026-07-31: remote MCP、ChatGPT developer mode、OAuth guidance。  
  https://developers.openai.com/api/docs/mcp
- OpenAI, **Connect and test your plugin**, read 2026-07-31: public HTTPS Streamable HTTP或Secure MCP Tunnel；公開submission仍需public HTTPS。  
  https://developers.openai.com/plugins/deploy/connect-chatgpt
- Claude Help Center, modified 2026-07-22: remote custom connectors available on Claude Desktop and other plans。  
  https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- MCP official docs version 2026-07-28: stdio and Streamable HTTP; Claude local/remote connection flows。  
  https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-remote-servers
- Kiro & IDE official MCP configs, read 2026-07-31: Kiro (`url`, `headers`, OAuth/DCR), Cursor (`.cursor/mcp.json` or global config), Windsurf (`.codeium/windsurf/mcp_config.json`), VS Code (`mcpServers` setting), Claude Code (`claude mcp add`).
  https://kiro.dev/docs/mcp/configuration/
- Official Python SDK v2.0.0, published 2026-07-28: `FastMCP` renamed `MCPServer`; `streamable_http_app()` owns session manager and transport security。  
  https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0

## 4. Component architecture

```text
 ChatGPT Desktop / ChatGPT cloud
        │ HTTPS + OAuth (or Secure MCP Tunnel in dev)
 Claude Desktop remote connector ───────┐
        │ HTTPS + OAuth                 │
 Kiro user/workspace MCP config ────────┤
        │ HTTPS localhost or remote     │
                                       ▼
┌──────────────────────────────────────────────────────────┐
│ recall-memory-mcp (single authority)                     │
│  ┌────────────────────────────────────────────────────┐  │
│  │ MCPServer v2 / Streamable HTTP `/mcp`             │  │
│  │ - session lifecycle, POST/GET/DELETE              │  │
│  │ - strict schemas + tool annotations               │  │
│  │ - Host/Origin allowlist + body limits             │  │
│  └───────────────┬────────────────────────────────────┘  │
│                  ▼                                       │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Auth + policy                                     │  │
│  │ OAuth scopes, actor/client identity, redaction    │  │
│  └───────────────┬────────────────────────────────────┘  │
│                  ▼                                       │
│  ┌────────────────────────────────────────────────────┐  │
│  │ MemoryService                                     │  │
│  │ validation, idempotency, revisions, provenance,   │  │
│  │ transaction boundary, soft delete, audit events   │  │
│  └───────────────┬────────────────────────────────────┘  │
│                  ▼                                       │
│  ┌────────────────────────────────────────────────────┐  │
│  │ RecallRepository adapter                          │  │
│  │ canonical recall-sqlite RRF + keyword/FTS/vector  │  │
│  └───────────────┬────────────────────────────────────┘  │
│                  ▼                                       │
│      one local-first SQLite DB (+ WAL, backups)          │
└──────────────────────────────────────────────────────────┘
```

## 5. Package/layout proposal

```text
recall/
├── src/recall/                         # canonical recall-sqlite core
├── tests/                              # canonical core tests
├── recall-memory-mcp/
│   ├── pyproject.toml                  # dist: recall-memory-mcp
│   ├── src/recall_memory_mcp/
│   │   ├── __init__.py
│   │   ├── cli.py                      # init/serve/doctor/client-config
│   │   ├── settings.py                 # validated config, no secrets echoed
│   │   ├── server.py                   # MCPServer v2 tools/annotations
│   │   ├── app.py                      # ASGI app, lifespan, host/origin policy
│   │   ├── service.py                  # use cases + transaction boundaries
│   │   ├── repository.py               # recall-sqlite adapter
│   │   ├── auth.py                     # OAuth resource-server integration
│   │   ├── models.py                   # request/result/domain types
│   │   ├── redaction.py
│   │   └── legacy_stdio.py             # optional bridge, not authority
│   ├── tests/
│   │   ├── test_tools.py
│   │   ├── test_protocol.py
│   │   ├── test_idempotency.py
│   │   ├── test_concurrency.py
│   │   ├── test_auth.py
│   │   ├── test_redaction.py
│   │   ├── test_migration.py
│   │   └── test_client_configs.py
│   └── clients/
│       ├── chatgpt/README.md
│       ├── chatgpt/PLUGIN_INSTRUCTIONS.md
│       ├── claude/README.md
│       ├── claude/INSTRUCTIONS.md
│       ├── kiro/mcp.example.json
│       └── kiro/recall-memory-steering.example.md
└── .kiro/specs/recall-mcp-cross-client/
```

### Naming

- PyPI `recall-mcp` 已被他人占用（查詢時間2026-07-31）。
- `recall-memory-mcp`與`recall-sqlite-mcp`當時未占用；發布前必須重查。
- Python import使用 `recall_memory_mcp`。

## 6. Transport design

### 6.1 Official SDK only

Pin compatible official SDK line：`mcp>=2.0,<2.1`，並依實測更新。v2使用：

```python
from mcp.server.mcpserver import MCPServer
```

`server.py`只定義 identity、instructions、tools；`app.py`呼叫 `streamable_http_app()`並負責top-level ASGI lifespan。不得把mounted sub-app自己的lifespan當成已執行。

### 6.2 Modes

| Mode | Bind | Auth | Clients | Purpose |
|---|---|---|---|---|
| local | `127.0.0.1` | local trusted policy + Host/Origin allowlist | Kiro local；protocol tests | 開發／單機 |
| tunnel-dev | localhost behind Secure MCP Tunnel | tunnel identity + development grant | ChatGPT developer mode；Claude/Kiro可共用URL | 三端E2E |
| remote | public HTTPS | OAuth 2.1 resource server | ChatGPT、Claude、Kiro | 正式發行 |

MVP固定使用stateful Streamable HTTP，支援 `POST`, `GET`, `DELETE`與 `Mcp-Session-Id`。`stateless_http`/JSON-only mode不在本版production contract，即使單一client可用也不得取代session tests；SSE視為legacy compatibility，不作主路徑。

### 6.3 Client interop probe contract

在鎖定SDK mode前，產生machine-readable `client-interop-matrix.json`，每個真host至少記錄：client/version、transport、endpoint path、OAuth registration path（CIMD/DCR/pre-registered）、required request/response headers、session behavior、tool annotations/schema接受情況、initialize → tools/list → tools/call → DELETE call trace與證據時間。未知欄位標`UNVERIFIED`，不得推定相容。

### 6.4 Transport security

- localhost host allowlist：`127.0.0.1`, `localhost`與實際port。
- remote allowlist：明確hostname/origin；禁止 `*`。
- Request body size、tool timeout、rate limit與concurrency limit皆有上限。
- `/health`只回status/version；SDK custom route不自帶auth，因此不得放private stats。

## 7. Tool design

| Tool | Annotation | Side effect | Scope |
|---|---|---|---|
| `memory_search` | readOnly=true, destructive=false | none；authority DB零mutation | read |
| `memory_get` | readOnly=true | none | read |
| `memory_recent` | readOnly=true | none | read |
| `memory_add` | readOnly=false, idempotent=true with key | create | write |
| `memory_replace` | readOnly=false, idempotent=true, destructive=false | new revision | write |
| `memory_remove` | readOnly=false, idempotent=true, destructive=true | tombstone | write |
| `memory_status` | readOnly=true | none；預設不回exact counts | read |

Tool result envelope：

```json
{
  "ok": true,
  "data": {},
  "meta": {
    "server_version": "...",
    "degraded": false,
    "request_id": "..."
  }
}
```

Memory result至少包含：

```json
{
  "memory_id": "...",
  "revision": 3,
  "content": "...",
  "scope": "project:recall",
  "kind": "decision",
  "tags": ["architecture"],
  "score": 0.82,
  "provenance": {
    "source_client": "kiro",
    "source_conversation": "optional",
    "created_at": "...",
    "updated_at": "..."
  },
  "data_trust": "untrusted_memory_content"
}
```

## 8. Data and migration design

### 8.1 Canonical core

Root `recall-sqlite` SHALL是唯一storage/retrieval正本。現有未追蹤 `recall-core`不得成為第二個永久fork；可做migration source，但所有行為最後落在root core與同一schema。

### 8.2 Schema additions

在現有 `memories`與indexes之外新增：

```sql
CREATE TABLE owners (
  owner_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE client_grants (
  grant_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  issuer TEXT NOT NULL,
  subject TEXT NOT NULL,
  client_id TEXT NOT NULL,
  grant_generation INTEGER NOT NULL,
  source_client TEXT NOT NULL,
  oauth_scopes_json TEXT NOT NULL,
  memory_scope_patterns_json TEXT NOT NULL,
  binding_challenge_id TEXT REFERENCES owner_link_challenges(challenge_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  reauthorized_at TEXT,
  revoked_at TEXT,
  unlinked_at TEXT,
  UNIQUE(issuer, subject, client_id, grant_generation)
);

CREATE TABLE owner_link_challenges (
  challenge_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  challenge_hash TEXT NOT NULL,
  oauth_state_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE owner_link_events (
  link_event_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  grant_id TEXT,
  challenge_id TEXT,
  operation TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  details_digest TEXT NOT NULL
);

CREATE TABLE embedding_profiles (
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  scope TEXT NOT NULL,
  generation INTEGER NOT NULL,
  provider TEXT NOT NULL,
  endpoint_identity_hash TEXT NOT NULL,
  model TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('BUILDING','ACTIVE','RETIRED','FAILED')),
  created_at TEXT NOT NULL,
  activated_at TEXT,
  retired_at TEXT,
  PRIMARY KEY(owner_id, scope, generation)
);

CREATE UNIQUE INDEX idx_embedding_profiles_one_active
  ON embedding_profiles(owner_id, scope)
  WHERE status = 'ACTIVE';

CREATE TABLE memory_metadata (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE RESTRICT,
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  creator_grant_id TEXT NOT NULL REFERENCES client_grants(grant_id) ON DELETE RESTRICT,
  revision INTEGER NOT NULL,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_client TEXT NOT NULL,
  source_conversation TEXT,
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE memory_embeddings (
  embedding_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  scope TEXT NOT NULL,
  generation INTEGER NOT NULL,
  provider TEXT NOT NULL,
  endpoint_identity_hash TEXT NOT NULL,
  model TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  embedding_blob BLOB NOT NULL,
  vector_rowid INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(owner_id, scope, generation)
    REFERENCES embedding_profiles(owner_id, scope, generation)
    ON DELETE CASCADE,
  UNIQUE(memory_id, generation)
);
CREATE INDEX idx_memory_embeddings_owner_scope_gen
  ON memory_embeddings(owner_id, scope, generation);

CREATE TABLE vector_tables (
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  scope TEXT NOT NULL,
  generation INTEGER NOT NULL,
  table_name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  FOREIGN KEY(owner_id, scope, generation)
    REFERENCES embedding_profiles(owner_id, scope, generation)
    ON DELETE CASCADE
);

CREATE TABLE memory_events (
  event_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL,
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  grant_id TEXT NOT NULL REFERENCES client_grants(grant_id) ON DELETE RESTRICT,
  revision INTEGER NOT NULL,
  operation TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  source_client TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  UNIQUE(memory_id, revision)
);

CREATE TABLE mcp_idempotency (
  owner_id TEXT NOT NULL REFERENCES owners(owner_id) ON DELETE RESTRICT,
  grant_id TEXT NOT NULL REFERENCES client_grants(grant_id) ON DELETE RESTRICT,
  actor_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  result_schema_version INTEGER NOT NULL,
  payload_digest TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  purged_at TEXT,
  PRIMARY KEY(grant_id, operation, idempotency_key)
);

CREATE INDEX idx_mcp_idempotency_memory
  ON mcp_idempotency(owner_id, memory_id);
```

每個connection SHALL先啟用 `PRAGMA foreign_keys=ON`。Migration SHALL：SQLite backup API snapshot → transaction migration → index integrity check → row count/hash sampling → reopen read-back。Authority初始化一個本機owner；legacy import使用受限migration grant。Existing rows轉成 `revision=1`, `scope=legacy:unscoped`, `kind=legacy`, `source_client=legacy-import`，不改原內容或ID；使用者明確分類前不得出現在global/project search。舊embedding provenance未知時保留NULL並標`reindex_required`，不得猜model/dimension。

### 8.3 Update/delete correctness

現有 `SQLiteStore.update()`不會重建keyword/FTS/vector indexes，不能直接給`memory_replace`使用。新增transactional repository operation，必須用同一connection與 `BEGIN IMMEDIATE` 同步更新content、embedding、keywords、FTS、metadata、event與idempotency result；不得在transaction內呼叫會另開connection的legacy store methods。Soft delete保留row與event，所有retrieval paths排除 `deleted_at IS NOT NULL`。

MCP read path使用`retrieve(update_access=False)`（或等價pure query adapter），不呼叫現行`store.touch()`，不因search改tier、access_count、revision或indexes。Adaptive access若未來需要，改由明確maintenance command處理並獨立測試。

Vector search先讀取`embedding_profiles`唯一ACTIVE row，再JOIN `memory_embeddings`以`owner_id + scope + generation`直接限制candidate SQL。Reindex建立全新BUILDING generation與獨立`memory_embeddings` rows與 generation-scoped `vector_tables`；不修改`memory_metadata`或舊向量。BUILDING期間之寫入/更新對ACTIVE與BUILDING執行dual-write；變更scope在單一`BEGIN IMMEDIATE` transaction原子更新`memory_metadata`與全世代`memory_embeddings`。完成row count/dimension/hash sampling後，在單一`BEGIN IMMEDIATE` transaction把舊ACTIVE改RETIRED、新generation改ACTIVE。失敗或取消標FAILED、清理BUILDING `memory_embeddings`與對應`vector_tables`並保留ACTIVE；keyword/FTS degraded path不得混入BUILDING/RETIRED vectors。

### 8.4 Idempotency/conflict

- Server以 `(grant_id, operation, idempotency_key)`查重；`actor_id`只作audit identity，不作idempotency namespace。
- Service先驗current token、grant generation未revoked/unlinked、owner一致且stored `scope`仍在current allowlist，之後才可查idempotency row或回`result_json`；任一授權失敗回統一not-authorized，不洩漏key是否存在。
- Active row的keyed payload digest不同 → `IDEMPOTENCY_KEY_REUSED`；不得保存raw content hash。
- `expected_revision != current_revision` → `REVISION_CONFLICT`並回傳current revision，不回內容除非caller有read scope。
- 寫入與idempotency result同transaction commit。
- Write tools缺少`idempotency_key`即validation error；MCP request ID只能作trace，不取代durable idempotency key。
- 每個write在side effect前先決定target `memory_id`（add亦先產生ID），並寫入`mcp_idempotency.memory_id`。Hard purge以indexed `(owner_id, memory_id)`將`result_json`設NULL、將payload改為keyed digest並寫`purged_at`；deny row保留期間內任何同grant/operation/key replay都回`IDEMPOTENCY_KEY_PURGED`，不解析`result_json`也不產生side effect。
- Purged deny rows至少保留到該grant永久revoked且超過文件化的最大client retry + backup retention window；MVP預設保留到owner明確執行完整identity reset，不提供一般GC自動刪除。

## 9. Authentication design

### Local

- 不把token寫進host JSON。
- 綁localhost；Host/Origin allowlist；可選OS user-bound local token，但不能靠CORS當auth。

### Remote

- MCP resource server發布protected-resource metadata。
- OAuth authorization code + PKCE；優先支援ChatGPT建議的Client ID Metadata Documents（CIMD），並容納DCR與ChatGPT/Claude/Kiro預註冊client差異。
- Access token audience綁定Recall resource；scope最小化。
- Client grants可獨立撤銷。
- Authorization server可先接成熟self-hosted方案；不自行發明password/token系統。
- `actor_id`由OAuth subject + client identity推導，`source_client`由已驗證client metadata推導；tool payload不得自行指定。
- Authority建立stable local `owner_id`；每次OAuth核准建立／更新`client_grants`映射。跨issuer帳號連結需owner在已登入local admin flow明確批准，不以email自動合併。
- Repository每次operation先由grant解析`owner_id`與allowed memory scope patterns，再將兩者放入SQL predicate；revoked grant在進service前拒絕。
- Owner linking flow使用local admin session產生短效、single-use pairing challenge，儲存challenge hash/expiry/used_at；OAuth callback驗`state`/PKCE/issuer後才建立grant。遠端callback不得自行指定owner_id，失效／重放challenge均fail closed。
- Initial bind、reauthorize、scope change、revoke與unlink都寫`owner_link_events` keyed digest audit。每次重新授權建立遞增`grant_generation`；舊generation保持revoked/unlinked且token與idempotency replay永久失效。同issuer/subject/client若已屬另一owner則fail closed，MVP不自動搬移owner。

## 9.1 Delete, restore, purge, and export lifecycle

- `memory_remove`只寫tombstone並遞增revision；一般MCP tools不列出內容。
- `recall-memory-mcp memory restore <id>`是owner-admin CLI；重建indexes並產生新revision/event。
- `recall-memory-mcp memory purge <id> --expected-revision N --confirm <id>`在單一`BEGIN IMMEDIATE` transaction依序：驗owner/scope且current revision恰為N → 保留`N+1`作final revision並寫唯一content-free purge event → scrub idempotency results為deny tombstones → 刪vector/keyword/FTS rows → 刪`memory_metadata`（解除`ON DELETE RESTRICT`）→ 刪`memories` row → commit。`UNIQUE(memory_id, revision)`保證purge不能重用既有revision；stale、constraint conflict或任一步失敗全部rollback。Event以自身`owner_id/grant_id`保留ID、final revision、keyed HMAC digest、時間、actor metadata，不保留raw content hash，也不依賴已刪memory row做owner歸屬。
- `export`預設排除tombstone，`--include-tombstones`必須明確；backup保留完整DB並受backup retention規則管理。
- `memory export/restore/purge` owner-scoped admin CLI永遠只呼叫online authority admin API，不存在同名offline/direct-DB fallback；authority從OS-bound local admin session或`memory:admin` token推導owner/scope。Export拒絕symlink/既有檔覆寫，建立owner-only檔；遠端或可移動destination要求加密，所有admin operations寫content-free audit event。
- Offline direct-DB只存在於明確`db backup`、`db restore --whole-database`、`db migrate` break-glass子命令；它是受信任本機OS operator邊界，**不提供也不宣稱MCP owner ACL**。CLI allowlist硬性禁止memory list/search/export/row-level restore/purge與grant edit；執行前須證明service停止、取得同一OS authority lock、顯示整庫影響、要求不可腳本誤觸的確認字串，執行後寫不含DB content/path secret的local operator audit。Whole-DB restore必須先驗backup integrity/version並再次backup current DB。
- `memory_status`預設只回status/version/capabilities。Exact owner/scope counts不進model tool；owner-admin diagnostics只有在最低cardinality門檻以上才回bucket（例如0、1–9、10–99、100+），防止小scope側錄。

## 10. Host adapters and honest automation

### ChatGPT Desktop

- Developer mode新增plugin/server URL。
- 本機開發用Secure MCP Tunnel；公開發行需public HTTPS。
- Plugin instructions鼓勵在需要背景時search、明確「記住」時add。
- 不宣稱桌面app可直接spawn local stdio server。

### Claude Desktop

- 主要路徑：Settings → Connectors → Custom Web → remote MCP URL。
- 可選local stdio bridge，但只代理authority。
- Connector instructions標出write/delete需明確意圖。

### Kiro

- User config：`~/.kiro/settings/mcp.json`；workspace config：`.kiro/settings/mcp.json`。
- Remote server使用 `url`、OAuth/DCR；server同時提供CIMD相容路徑，不得把secret hardcode或commit。
- `autoApprove`只能包含read-only tools；write/remove不得autoApprove。
- Steering template要求先search再規劃，只有durable facts才add。

## 11. Failure model

| Failure | Required behavior |
|---|---|
| Authority down | client收到typed unavailable；不建立shadow DB |
| Embedding down | keyword/FTS degraded mode + metadata flag |
| OAuth unavailable/expired | 401 + metadata discovery；不fallback匿名 |
| DB locked | bounded retry/backoff後typed busy error；不重複commit |
| Stale revision | conflict；不last-write-wins |
| Duplicate request | replay原result；不新增第二筆 |
| Index write fails | whole transaction rollback |
| Malicious memory content | structured untrusted result；server不執行 |
| Secret in exception | redacted error + internal request ID |
| Host不呼叫tool | UI/trace顯示未呼叫；不宣稱已同步 |
| 第二個authority process | DB ownership lock拒絕啟動；不共用同一SQLite檔 |

Authority lock使用OS-backed exclusive advisory lock，由process lifetime持有；crash時由OS釋放。Lock metadata（instance_id、start time、redacted DB identity）只供診斷，不能單靠PID/stale file判斷接管。新process只有成功取得OS lock後才可重寫metadata；network filesystem與不支援可靠locking的路徑在MVP fail closed。

## 12. Verification architecture

1. **Unit/property:** validation、redaction、idempotency、revision、soft delete。
2. **Repository integration:** real temp SQLite、WAL、index parity、migration rollback。
3. **Protocol:** official Python SDK Client、MCP Inspector、POST/GET/DELETE/session lifecycle。
4. **Concurrency:** 三個process barrier，同key、不同key、stale update、server restart。
5. **Security:** host/origin/DNS rebinding、OAuth scopes、expired/revoked token、oversized body、prompt-injection payload。
6. **Client configs:** JSON parse、environment placeholders、no secrets、read-only autoApprove only。
7. **True host E2E:** ChatGPT Desktop、Claude Desktop、Kiro逐端tool discovery/call/read-back。
8. **Release:** clean build、sdist、wheel、hash、CI、maker ≠ grader fresh-context reviewer、PyPI/GitHub read-back。
9. **Operations:** SQLite backup API、corrupt backup拒絕restore、authority service restart與single-owner lock。
10. **Isolation:** 兩個owner、三個grant、跨scope ID guessing；所有未授權read/write均零side effect。
11. **Read purity:** search前後DB content/revision/tier/index hash不變，並與concurrent replace/remove交錯測試。

## 13. Deployment decision gates

- **Gate A:** official SDK protocol tests通過前，不接真host。
- **Gate B:** local Kiro + IDE agents (Cursor/Windsurf/VS Code/Claude Code) + Claude cross-client canary通過前，不開ChatGPT tunnel。
- **Gate C:** ChatGPT developer mode canary通過前，不宣稱三端同步。
- **Gate D:** OAuth、host allowlist、redaction、revocation通過前，不公開Internet endpoint。
- **Gate E:** 三端真實write/read/replace/remove read-back + independent review通過前，不發布1.0或「安裝即同步」文案。
### IDE Agents (Cursor, Windsurf, VS Code, Claude Code)

1. **Configuration matrix:**
   - **Cursor:** `.cursor/mcp.json` 或全域 MCP 設定（支援 sse / streamable HTTP 與 stdio proxy）。
   - **Windsurf:** `mcp_config.json`（支援 stdio 與 HTTP transport）。
   - **VS Code:** `settings.json` 中的 `mcpServers` 或 MCP Extension。
   - **Claude Code CLI:** `claude mcp add recall -- http://...` 或 local stdio adapter。
2. **Identity & Auth:** 本機開發時經 stdio proxy 帶入 OS local session 授權；遠端/隊列模式經 Streamable HTTP 帶 Bearer Token / OAuth grant。
3. **Scope mapping:** IDE agents 預設帶入當前 workspace project slug（例如 `project:recall-memory`）與 `global` 組合，確保 code context 記憶自動隔離。

