# Recall MCP Cross-Client Shared Memory — Implementation Tasks

> **For Hermes/Kiro:** 依序執行；每個phase完成後先過gate與fresh-context review。勾選task只代表文件狀態，不能取代測試／外部read-back。

**Goal:** 交付一個 `recall-memory-mcp`，讓ChatGPT Desktop、Claude Desktop與Kiro透過同一 authority共享可追溯記憶。

**Status:** 所有項目皆為TARGET；現有手寫stdio/HTTP MCP只能當migration input。

## Phase 0 — Baseline and source-of-truth

- [x] 0.1 備份即將修改的既有檔至 `artifacts/recall-mcp-cross-client/backups/`，不得備份DB內容到Git。
- [x] 0.2 在全新shell分開跑tracked root core tests與mixed `tests/` suite；保存真實輸出，不得把dependency/import errors記成pass。
- [x] 0.2a 建立test/dependency ownership matrix，標出root package、`recall-core`、`recall-server`與MCP distribution各自應宣告的runtime/dev dependencies。
- [x] 0.3 建立 `artifacts/recall-mcp-cross-client/current-state.json`，記錄Git HEAD、tracked/untracked boundaries、Python/MCP SDK版本與測試結果。
- [x] 0.4 建立claim ledger，將`ARCHITECTURE.md`的完成宣稱逐項對照Git-tracked source/tests。
- [x] 0.5 決定並文件化canonical core：root `src/recall/`為正本；列出`recall-core/`與`recall-server/`可吸收／可刪／需遷移內容。
- [x] 0.6 重新查PyPI/GitHub名稱；確認`recall-memory-mcp`可用且不冒充既有`recall-mcp`。
- [x] 0.7 建立feature branch；不得把既有無關untracked files一起stage。
- [x] 0.8 建立`artifacts/recall-mcp-cross-client/client-interop-matrix.json` schema；ChatGPT/Claude/Kiro 與 IDE 代理 (Cursor/Windsurf/VS Code/Claude Code) 各自由驗證欄位。

**Gate 0:** tracked core baseline通過；mixed suite既有失敗已逐項分類並指派到後續phase；canonical source決策完成；無secret/private DB被納入。

## Phase 1 — Core schema and transactional repository (TDD)

### 1A. Migration

- [x] 1.1 在 `tests/test_mcp_schema_migration.py`寫失敗測試：legacy DB升級後原ID/content/count保持。
- [x] 1.2 在 `src/recall/migrations.py`實作versioned migration runner與schema version table。
- [x] 1.3 新增`owners`, `client_grants`, `owner_link_challenges`, `owner_link_events`, `embedding_profiles`, `memory_embeddings`, `vector_tables`, `memory_metadata`, `memory_events`, `mcp_idempotency` migration、active-profile partial unique index與`(memory_id, generation)` unique index與`idx_memory_embeddings_owner_scope_gen`覆蓋索引；idempotency row含scope/result schema，event/idempotency各自保存owner/grant。
- [x] 1.4 測試migration可重跑、途中失敗rollback、備份後reopen read-back。
- [x] 1.5 測試legacy rows取得revision=1/legacy:unscoped/legacy provenance且不改內容，也不出現在global/project search。
- [x] 1.5a 測每個owner+scope只能一個ACTIVE embedding generation；BUILDING generation與ACTIVE並存時，寫入新`memory_embeddings`不影響ACTIVE搜尋；測BUILDING期間add/replace寫入dual-write至所有非RETIRED世代，且scope變更時原子同步全世代`memory_embeddings.scope`；cutover失敗自動清理BUILDING向量與`vector_tables`並保留舊ACTIVE。

### 1B. Transactional CRUD

- [x] 1.6 在 `tests/test_mcp_repository.py`寫add transaction與index parity失敗測試。
- [x] 1.7 在 `src/recall/mcp_repository.py`建立`RecallMCPRepository`；不得複製整套store。
- [x] 1.8 實作transactional add：同一connection + `BEGIN IMMEDIATE`完成memory + embedding + keyword + FTS + metadata + event + idempotency result；啟用foreign keys，禁止nested legacy connections。
- [x] 1.9 實作replace；同步重建所有索引並遞增revision。
- [x] 1.10 實作soft remove；一般search/get/recent排除tombstone。
- [x] 1.10a 實作owner-admin restore與hard purge transaction；purge要求`expected_revision=N`、寫唯一final event revision `N+1`，再scrub idempotency result為deny tombstone、刪vector/keyword/FTS、metadata、memory row，任一步失敗whole rollback。
- [x] 1.10b 測purge後同grant/operation/idempotency key重送add/replace/remove皆回`IDEMPOTENCY_KEY_PURGED`且不重建內容；deny row不含result_json/raw content hash。
- [x] 1.10c 測add=revision1、remove=revision2、restore=revision3、purge expected3→final event revision4；purge expected2與重複revision4皆fail closed且保留原資料。
- [x] 1.11 實作get/search/recent；每個repository method的SQL predicate都必須包含`owner_id` + authorized exact scope + deleted filter，不得先查出再只在Python過濾。
- [x] 1.11a 改造MCP retrieval為`update_access=False` pure read path，不呼叫`store.touch()`或tier mutation；search前後DB revision/tier/index hash相同。
- [x] 1.11b 以SQL trace/mutation tests覆蓋get/search/recent/replace/remove/restore/purge，證明每條read/write path都有owner+scope predicate。
- [x] 1.12 測試index寫入任一步失敗會whole-transaction rollback。

### 1C. Concurrency/idempotency

- [x] 1.13 寫兩個real SQLite connection + thread/process barrier regression。
- [x] 1.14 驗同idempotency key同payload只commit一次並replay同result。
- [x] 1.15 驗同key不同payload fail closed。
- [x] 1.16 驗兩個replace使用同expected_revision只有一個成功。
- [x] 1.17 驗server restart後idempotency仍成立。
- [x] 1.17a 驗idempotency lookup前先做current auth：revoked/unlinked grant、scope downgrade、wrong owner以舊key replay皆回統一not-authorized、零內容／零key-existence洩漏／零side effect。
- [x] 1.18 實作OS-backed authority DB ownership lock；第二個process fail closed，crash後OS自動釋放且新process可安全接管；不得用PID file刪除當互斥鎖。
- [x] 1.19 以兩個owner、三個grant測cross-owner/cross-scope ID guessing、search、replace、remove皆fail closed。
- [x] 1.20 測pure search與replace/remove並行不造成lost update、tier/index drift。

Run：`python -m pytest -q tests/test_mcp_schema_migration.py tests/test_mcp_repository.py tests/test_mcp_concurrency.py tests/test_authority_lock.py`

**Gate 1: PASS** (migration、transaction、index parity、concurrency全PASS；maker ≠ grader審查schema與data-loss風險。)


## Phase 2 — New MCP distribution skeleton

- [x] 2.1 建立 `recall-memory-mcp/pyproject.toml`，distribution=`recall-memory-mcp`，import=`recall_memory_mcp`。
- [x] 2.2 Pin `mcp>=2.0,<2.1`與compatible `recall-sqlite`版本；不得依賴未發布的`recall-core`。
- [x] 2.3 建立 `recall-memory-mcp/src/recall_memory_mcp/__init__.py`與單一version source。
- [x] 2.4 建立 `models.py`的strict request/result types與length/range constraints。
- [x] 2.5 建立 `settings.py`：預設localhost、DB/config paths、host/origin allowlist；repr/dump redacted。
- [x] 2.6 建立 `redaction.py`並測試token、Bearer、URL credentials、Windows paths與traceback不外洩。
- [x] 2.7 建立 `service.py`，只依賴repository protocol，不依賴MCP transport。
- [x] 2.8 建立`tests/test_models.py`, `test_settings.py`, `test_redaction.py`, `test_service.py`。

Run：`python -m pytest -q recall-memory-mcp/tests/test_models.py recall-memory-mcp/tests/test_service.py recall-memory-mcp/tests/test_redaction.py`

**Gate 2: PASS** (service可以無network測試；package fresh import成功；無secret預設值。)

## Phase 3 — Official MCP SDK v2 server

- [x] 3.1 建立 `server.py`，使用 `from mcp.server.mcpserver import MCPServer`；不得手寫protocol dispatcher。
- [x] 3.2 實作`memory_search`與空query/limit/scope tests。
- [x] 3.3 實作`memory_get`與not-found typed error。
- [x] 3.4 實作`memory_recent`，禁止空query隱式dump。
- [x] 3.5 實作`memory_add`與idempotency annotation/schema。
- [x] 3.6 實作`memory_replace`與expected_revision conflict。
- [x] 3.7 實作`memory_remove` soft-delete與destructive annotation。
- [x] 3.7a 驗三個write tools都把`idempotency_key`列為required；缺值fail closed且不產生side effect。
- [x] 3.8 實作`memory_status`，只回redacted capability/health；預設不回exact count，admin diagnostics只回達cardinality門檻的bucket。
- [x] 3.9 每個memory result加入provenance、score與`data_trust=untrusted_memory_content`。
- [x] 3.10 建立 `tests/test_tools.py`驗tool list、schemas、annotations與structured outputs。
- [x] 3.11 用官方SDK client做stdio-free in-process protocol tests。
- [x] 3.12 鎖定MVP `stateful Streamable HTTP` capability；若SDK config開啟stateless/json mode，contract test必須失敗。

Run（需 `mcp==2.0.0` runtime）：`python -m pytest -q recall-memory-mcp/tests/test_tools.py recall-memory-mcp/tests/test_inprocess_protocol.py`

**Gate 3: PASS** (官方 `mcp.client.Client` in-process 對 7 個 tools 全數 list + call 成功；evidence `artifacts/recall-mcp-cross-client/evidence/phase3-tools-list.json` 的 leak scan 命中 0；SDK 自身的 argument-validation 文字由 server middleware 統一改寫，raw exception/path/secret 均不出 wire。)

## Phase 4 — Streamable HTTP application

- [x] 4.1 建立 `app.py`，由`MCPServer.streamable_http_app()`產生endpoint。
- [x] 4.2 正確接top-level ASGI lifespan與`session_manager.run()`；新增startup regression。
- [x] 4.3 支援`POST/GET/DELETE`與`Mcp-Session-Id`，不得固定回GET 405。
- [x] 4.4 加local Host/Origin allowlist、DNS-rebinding protection與body-size limit。
- [x] 4.5 加`/health` custom route，只回status/version，不回DB path/count/content。
- [x] 4.6 建立 `tests/test_protocol.py`驗initialize、tools/list、tools/call、session close與bad protocol。
- [x] 4.7 建立 `tests/test_transport_security.py`驗bad Host/Origin、oversized body、CORS headers。
- [x] 4.8 用MCP Inspector對fresh server跑list/call smoke，保存輸出。

Run：`python -m pytest -q recall-memory-mcp/tests/test_protocol.py recall-memory-mcp/tests/test_transport_security.py`

**Gate 4: PASS** (official protocol與security tests全238 passed；Streamable HTTP ASGI server、Lifespan、Transport Security、DNS-rebinding、Body limit、CORS全數綠燈驗證。)

## Phase 5 — OAuth and client identity

- [x] 5.1 依MCP 2026-07-28 authorization spec建立resource metadata與auth interface。
- [x] 5.2 選成熟OAuth authorization server／library，寫decision record；不得自製password flow。
- [x] 5.3 實作`memory:read`, `memory:write`, `memory:admin` scope enforcement。
- [x] 5.4 token audience、expiry、revocation與PKCE tests。
- [x] 5.5 CIMD、DCR與pre-registered client三條test matrix，覆蓋ChatGPT/Claude/Kiro差異。
- [x] 5.6 每個grant由OAuth subject + verified client metadata映射stable actor/source_client；忽略／拒絕payload冒充，且client撤銷不影響其他client。
- [x] 5.6a 實作stable local owner與`client_grants`；跨issuer account linking只能由已登入owner admin明確批准，不依email/name自動合併。
- [x] 5.6b 實作短效single-use pairing challenge與OAuth state/PKCE/issuer驗證；測remote owner_id injection、expired/replayed challenge、CSRF全部fail closed。
- [x] 5.6c 實作grant generation與`owner_link_events`：initial bind/reauthorize/scope change/revoke/unlink均可稽核；舊generation永久失效，跨owner issuer/subject/client collision fail closed。
- [x] 5.6d 測revoke、unlink、scope downgrade、重新授權後舊token與舊idempotency key都不可取得cached result；新grant不復活舊generation。
- [x] 5.7 禁止remote mode無auth啟動；local-only例外需明確flag且只能bind loopback。
- [x] 5.8 建立`tests/test_auth.py`與`test_scope_authorization.py`。

**Gate 5: PASS** (expired/revoked/wrong-audience/insufficient-scope全數251 passed綠燈；log/wire無token洩漏。)

## Phase 6 — CLI and installation UX

- [x] 6.1 建立 `cli.py`與console script `recall-memory-mcp`。
- [x] 6.2 實作`init`：拒絕覆寫；建立config/DB前顯示paths；完成後reopen read-back。
- [x] 6.3 實作`serve`：預設127.0.0.1；remote mode要求HTTPS/auth/allowlist prerequisites。
- [x] 6.4 實作`doctor`：SDK/version、DB/index、embedding、auth metadata、port；輸出redacted。
- [x] 6.5 實作`client-config`只輸出template；`--write`才修改host config，且先backup＋JSON read-back。
- [x] 6.6 建立 `tests/test_cli.py`與`test_client_configs.py`，使用isolated HOME/TEMP。
- [x] 6.7 clean wheel install後執行`init`, `doctor`, `serve --help` smoke。
- [x] 6.8 實作offline break-glass `db backup`、`db restore --whole-database`與`db migrate` allowlist；使用SQLite backup API、integrity/version check、service-stop + authority lock + explicit confirmation + content-free operator audit，禁止live DB普通copy。
- [x] 6.9 實作`service install/status/uninstall`或接既有supervisor；驗crash/restart後authority與ownership lock正常。
- [x] 6.10 實作`memory export/restore/purge --expected-revision N` admin CLI與backup retention警告；purge依indexed memory_id scrub全部idempotency result為content-free deny tombstone，並以DB content scan、舊key replay、final revision event、external read-back證明不可還原也不可重建。
- [x] 6.10a Owner-scoped Admin CLI永遠只走online authority、由OS-bound local admin或`memory:admin` grant推導owner；測wrong owner/revoked grant與任何memory direct-DB fallback皆拒絕。
- [x] 6.10b 測export owner/scope isolation、tombstone policy、owner-only destination、symlink/overwrite拒絕與離機加密要求；purge後restore嘗試fail closed並提示backup retention責任。
- [x] 6.10c 測offline command allowlist只接受整庫backup/verify-restore/migrate；memory list/search/export/row-level restore/purge、grant edit全拒絕，且break-glass warning/confirmation/operator audit可read-back。

**Gate 6: PASS** 零知識使用者可安裝、初始化、診斷；不需要手填DB path或把secret寫入repo。
（clean-wheel `evidence/phase6-gate6-check.txt`：`--help`列出全部子命令；`init`先印路徑、拒絕覆寫、read-back；重跑`init`exit 3；`doctor --json`全綠且無絕對路徑/secret；`client-config`只印範本；`service status`未註冊任何OS unit；offline allowlist對 export/purge/memory-export/grant-edit/sql 全部 exit 2；`memory --help`無`--owner-id`；`config.toml`無任何secret賦值。）

## Phase 7 — Host integration assets

### ChatGPT Desktop

- [x] 7.1 建立 `clients/chatgpt/README.md`，只引用當前OpenAI developer mode流程。
- [x] 7.2 建立`PLUGIN_INSTRUCTIONS.md`，定義search/add/replace/remove何時呼叫，禁止全對話自動保存。
- [ ] 7.3 用Secure MCP Tunnel連本機server；保存endpoint/auth discovery evidence，不保存token。
- [ ] 7.4 真ChatGPT Desktop完成tool discovery與read/write canary。
- [ ] 7.4a 把ChatGPT client/version、CIMD/OAuth path、headers、session/call trace寫入interop matrix。

### Claude Desktop

- [x] 7.5 建立 `clients/claude/README.md`與`INSTRUCTIONS.md`。
- [ ] 7.6 真Claude Desktop Custom Web connector連同一URL並完成OAuth。
- [ ] 7.7 驗read/write與grant撤銷。
- [ ] 7.7a 把Claude client/version、OAuth registration path、headers、session/call trace寫入interop matrix。

### Kiro

- [x] 7.8 建立 `clients/kiro/mcp.example.json`，remote URL + OAuth；secret只用env placeholder。
### Phase 7 — E2E validation & canary read-back (Kiro & IDE Agents)

- [ ] 7.11 真Kiro連同一URL並完成read/write canary。
- [ ] 7.11a 把Kiro client/version、DCR/OAuth path、headers、session/call trace寫入interop matrix。
 - [x] 7.12 生成 IDE 整合配置範例：`docs/ide-mcp-setup.md`（含 Cursor `.cursor/mcp.json`、Windsurf `mcp_config.json`、VS Code `settings.json` 與 Claude Code `claude mcp add`）。
 - [ ] 7.12a 執行 IDE 代理（Cursor / Windsurf / VS Code / Claude Code）真機 MCP 呼叫測試，驗證工具發現與 `project:<slug>` scope 自動綁定，並寫入 interop matrix。

**Gate 7:** 三端都對同一authority完成真tool call；`connected`不能代替call/read-back。

> **7.1/7.2/7.5/7.8/7.12 completion basis (2026-08-05):** 82 asset tests green in
> the checkout; 11/11 mutations caught by `phase7_asset_mutation_probe.py`;
> 38/38 claims grounded in a live vendor fetch by `phase7_claim_grounding.py`
> (with its own negative control); the assets survive an sdist build and the
> suite is honest (71 passed / 11 skipped-with-reason, 0 failed) from an
> unpacked non-Git sdist. Evidence: `evidence/phase7-claim-grounding.{txt,json}`,
> `phase7-asset-mutation-probe.txt`, `phase7-sdist-asset-readback.txt`.
>
> **7.3/7.4/7.4a/7.6/7.7/7.7a/7.11/7.11a/7.12a stay unchecked.** They require an
> interactive OAuth login in a real ChatGPT Desktop / Claude Desktop / Kiro / IDE
> agent, which a headless scheduled run cannot perform. Writing the assets does
> not discharge them, and `connected` would not either — only a cross-client
> read-back does. Gate 7 is therefore NOT reachable from automation alone.

## Phase 8 — Cross-client consistency E2E

- [x] 8.1 建立唯一canary scope與content，不使用任何個資／secret。
- [ ] 8.2 Kiro `memory_add`，記錄memory_id/revision=1/request_id。
- [ ] 8.3 Claude `memory_get/search`，read-back同ID/revision/content hash。
- [ ] 8.4 ChatGPT以expected_revision=1 `memory_replace`，取得revision=2。
- [ ] 8.5 Kiro與Claude read-back revision=2；舊內容不得返回。
- [ ] 8.6 Claude `memory_remove` expected_revision=2，取得tombstone revision=3。
- [ ] 8.7 三端一般search均查不到；admin audit只看到tombstone metadata。
- [ ] 8.8 重送每個write的idempotency key，驗無duplicate/event drift。
- [ ] 8.9 中途restart authority後重跑read/idempotency。
- [ ] 8.10 將client call trace、server audit IDs與DB verification存入artifact；不得存token或完整私人記憶。

**Gate 8:** 上述十步全部有外部read-back；才可宣稱「ChatGPT Desktop、Claude Desktop、Kiro共享記憶」。

> **8.1 completion basis (2026-08-07):** `recall_memory_mcp/canary.py` +
> `tests/test_canary.py` (31 tests) build one unique `project:recall-canary-<12
> hex>` scope and its revision-1/revision-2 content. Safety is proven twice:
> the content survives the project's own `redact_text` byte-for-byte, and an
> independent 18-detector PII/secret scanner (with a negative control that
> requires every detector to fire on hostile input) reports zero findings on
> every value in the artifact. 5000 generated plans produced 5000 distinct
> scopes and 0 unsafe plans. `phase8_canary_mutation_probe.py` injects 17
> defects into `canary.py`; 16 must and do turn the suite RED, 1 is declared
> EQUIVALENT with proof (the explicit `run_id` guard is redundant with the
> pydantic `run_id`/`scope` patterns) and is paired with a "both guards
> removed" mutation that is caught. Artifact + read-back:
> `artifacts/recall-mcp-cross-client/canary/phase8-canary.json`,
> `evidence/phase8-canary-readback.txt` (15/15 checks re-verified against the
> bytes on disk), `evidence/phase8-canary-mutation-probe.txt`.
>
> **8.2 - 8.10 stay unchecked.** The suite's end-to-end test is one in-process
> SDK client against a fake repository: it proves the canary is *accepted* by
> the shipped tool surface, not that three real hosts share memory. Gate 8
> still requires interactive OAuth in ChatGPT Desktop / Claude Desktop / Kiro,
> which a headless scheduled run cannot perform.

## Phase 9 — Legacy migration and deprecation

- [x] 9.1 為既有`src/recall/recall_mcp.py`加deprecation test與新命令指引。
- [x] 9.2 若保留stdio，改成代理同一authority；不得直接開另一個primary DB。
- [x] 9.3 將可用的untracked`recall-server` REST/UI功能經獨立review後逐項吸收；禁止整包直接stage。
- [x] 9.4 更新`ARCHITECTURE.md`，把未驗證✅改成PARTIAL/TARGET；只有Gate evidence可標完成。
- [x] 9.5 更新README安裝、client matrix、auto-call限制、security與uninstall/backup流程。

**Gate 9: PASS** (public docs與tracked code完全一致；不存在兩個production MCP入口或兩套canonical core。)

## Phase 10 — Packaging, CI, and release

- [x] 10.1 建立source/version sync checker與retired-tag denylist。
- [x] 10.2 GitHub CI跑Windows/Linux × supported Python，含protocol/concurrency/package tests。
- [x] 10.3 clean build wheel/sdist；`twine check`；unpacked non-Git sdist全測。
- [x] 10.4 fresh venv從wheel安裝；確認module path在該venv site-packages。
- [x] 10.5 static secret/private-path scan與dependency audit。
- [x] 10.6 fresh-context maker ≠ grader進行spec compliance、security、protocol review。
- [ ] 10.7 PR CI全綠後merge/tag；trusted publishing。
- [ ] 10.8 PyPI JSON、download digest、clean install、GitHub Release asset digest外部read-back。
- [ ] 10.9 發布前再次驗名稱與官方client docs未變；若SDK/client契約變更，回Gate 3/7。

> **10.3 re-verified 2026-08-07 after a real defect was found.** The original
> tick rested on `evidence/phase7-sdist-asset-readback.txt`, which ran only
> `tests/test_client_assets.py` from the unpacked sdist — the one module that
> does not import `tests/_support.py`. Running the *whole* suite from the
> sdist failed with `ModuleNotFoundError: No module named '_support'`
> (7 collection errors), because `MANIFEST.in` relied on setuptools' default
> rule, which only matches `tests/test*.py`. Fixed with `graft tests` +
> `global-exclude __pycache__ *.py[cod]`, and a
> `Unpacked non-Git sdist full suite (task 10.3)` step was added to the
> `package` CI job (ubuntu + windows) so it cannot regress unnoticed.
> After-state: `721 passed, 16 skipped` from the unpacked non-Git sdist, with
> an import-origin read-back proving the sdist copy — not the checkout — was
> executed. Evidence: `evidence/phase8-sdist-full-suite-readback.txt`.

> **10.9 attempted 2026-08-07, deliberately NOT ticked.** The check is now
> implemented and mutation-proven, but the run itself was incomplete, and 10.9
> is only meaningful as a *complete* run immediately before a release.
>
> Implementation: `recall_memory_mcp/release_precheck.py` (network-free
> decision logic) + `tests/test_release_precheck.py` (56 tests) + the live
> driver `scripts/phase10_9_release_precheck.py`. The logic is fail-closed by
> construction: completeness is measured against the *expected* source set, so
> a source that is dropped or times out yields `INCOMPLETE`, never `PASS`; an
> unreachable registry is `observed=false` rather than either a pass or a
> naming violation; and a report is bound to a commit plus a 24 h expiry, so it
> cannot authorise a release cut from a later commit.
> `phase10_9_precheck_mutation_probe.py` injects 32 fail-open defects:
> 30 must and do turn the suite RED, 2 are declared EQUIVALENT with proof and
> each is paired with a caught partner. The probe's first run found two real
> escapes (a pre-release inside the numeric range, and a pin with no parseable
> constraint); both are now covered.
>
> Live result (`evidence/phase10.9-precheck.{txt,json}`, 2026-08-07T02:14:15Z,
> HEAD `ad1f086`): **VERDICT INCOMPLETE, exit 2, tickable=false.**
> - Name: `recall-memory-mcp` still 404 on PyPI JSON *and* Simple; `recall-mcp`
>   still 200. GitHub owner path **unreachable**, so unverified this round.
> - SDK: `mcp` latest on PyPI is `2.0.0`; the pin is `mcp>=2.0,<2.1` → still in
>   range, so **Gate 3 is not reopened**.
> - Client docs: only 3 of 10 vendor documents answered (Anthropic ×2,
>   Microsoft ×1). `claude-remote-mcp` UNCHANGED; `claude-code-mcp` and
>   `vscode-mcp-servers` show digest drift, but all 12 claims attributable to
>   the reachable documents are still grounded, so no contract break is visible
>   and **Gate 7 is not reopened by what was observed**. 26 of 38 claims could
>   not be checked at all.
> - Blocker: TCP connect to `developers.openai.com`, `kiro.dev`, `cursor.com`,
>   `docs.windsurf.com`, `docs.devin.ai` and `api.github.com` timed out on all
>   three attempts, from three different clients (urllib, curl, curl --ipv4).
>   DNS resolves; the failure is at the network path, not DNS or auth.
>
> 10.9 must be re-run when those hosts are reachable, and in any case
> immediately before publishing — the evidence file carries its own expiry.

**Release stop condition:** 任一真host未通過、OAuth未通過、或只證明tool discovery而無cross-client read-back時，不得發布「安裝即同步記憶」宣稱。
