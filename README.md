# Recall SQLite

Recall 是自製的 SQLite 長期記憶核心：保存文字、以向量／關鍵字／FTS5 混合檢索，並提供 Hot/Warm/Cold tier 管理。它不是生成式記憶服務，也不自動把整段對話變成可信事實。

本分支為 **`recall-sqlite 0.2.1` GitHub 交付候選**，配對 **`recall-memory-hermes 0.3.1`**。版本號不表示已上架 PyPI。GitHub 程式交付、PyPI 發布與使用者的 provider 切換是三件不同的事。

## 安裝與可重現驗證

Python 3.10+；本機驗收使用 Python 3.11。Windows 用 Git Bash 或 PowerShell；下列是 Git Bash／POSIX shell 指令。

```bash
python -m venv .venv
# Windows Git Bash：
source .venv/Scripts/activate
# Linux/macOS 改用：source .venv/bin/activate
python -m pip install build
python -m build --outdir ci-dist/core
python -m pip install ci-dist/core/recall_sqlite-0.2.1-py3-none-any.whl
```

這個候選尚未發布，請安裝自己建置的 wheel，不要把 `pip install recall-sqlite` 從 index 取到的既有版本當成此分支。正式發布後才可使用 `python -m pip install recall-sqlite==0.2.1`。

### Quick start：明確 DB、跨程序讀回

```bash
mkdir -p demo-data
export RECALL_DB_PATH="$PWD/demo-data/recall.db"
# 故意使用不可達的本機 endpoint，驗證無 embedding 時的 fallback。
export EMBED_BASE_URL=http://127.0.0.1:65534
recall add "cedar-17 demo prefers docker-compose" --session demo --tag semantic
recall query "cedar-17" --include-cold
recall stats --verbose
# 每個 CLI 指令都是新程序；再次 query 仍讀取同一 explicit DB。
recall query "cedar-17" --include-cold
```

PowerShell 等效設定：`$env:RECALL_DB_PATH = (Join-Path $PWD 'demo-data/recall.db')`。請先建立父目錄。CLI 沒有 `--db` 選項；使用 `RECALL_DB_PATH`，並在每個新 shell 設定同一路徑。

**預設 DB 不同，不能混用：**

- Core CLI：`RECALL_DB_PATH` > `DATA_DIR/recall_p0.db`；未設定 `DATA_DIR` 時，是 `recall.config` 所在檔案往上三層的 `recall_p0.db`（wheel 通常為 venv 的 `Lib/recall_p0.db`，不是目前目錄）。
- Hermes adapter：初始化時的 active `hermes_home/recall.db`，或 provider 的 explicit `db_path`。
- 要從 CLI 查看 Hermes 的庫，必須把 `RECALL_DB_PATH` 指向 adapter 的實際 `db_path`。不要依賴兩者預設相同。

Python API：

```python
from recall import Memory, SQLiteStore, retrieve_relevant
store = SQLiteStore("demo-data/recall.db")
store.add(Memory(content="cedar-17 demo uses docker-compose", tag="semantic", session_id="demo"))
results = retrieve_relevant("cedar-17", store, k=5, tag_filter="semantic")
print([memory.content for memory in results])
```

## Embedding 與限制

核心預設 `http://127.0.0.1:1234`、`nomic-embed-text-v1.5`，透過 OpenAI-compatible `/v1/embeddings` 呼叫；可設定 `EMBED_BASE_URL`、`EMBED_PORT`、`EMBED_MODEL`。預設向量維度是 **768**，換模型前需確認維度與既有 DB 一致。

- Endpoint 不可用時使用可用的 keyword／FTS5 路徑；不保證語意相同但無詞彙重疊的查詢命中。
- CLI `add` 不產生向量；Python／adapter 呼叫 `embed()` 後才會寫向量。沒有背景自動 backfill。
- 未命中 RRF 時會有有限範圍的 `get_all` fallback，可能返回不相關內容；結果必須核對來源。
- FTS 使用 `porter unicode61`，不是 jieba 或中文語意分詞。
- `session_id_filter` 是 optional，並保留 empty-session legacy 記憶；不是安全／tenant 授權邊界。Adapter 的 project 過濾也不是 ACL。
- 檢索可更新存取次數／tier；`delete`、`clear`、`gc` 與超過核心 GC 水位後的寫入可能刪除資料。正式庫先備份，這輪不執行清理。
- 不承諾固定延遲、不宣稱優於其他記憶系統；舊 benchmark 不是本候選的 A/B 證據。

## 實作範圍與規格邊界

| 範圍 | 本候選狀態 |
|---|---|
| `src/recall/store.py`, `retrieve.py`, `embed.py`, `cli.py` | 可安裝的 legacy SQLite 核心；本機測試／wheel smoke 覆蓋 |
| sqlite-vec `k` constraint、session filter、retrieval score、cold promotion 邊界 | fetched source 已存在；保存並新增真實 sqlite-vec 回歸測試，不用舊 runtime 覆蓋 |
| `migrations.py`, `mcp_repository.py`, `authority_lock.py` | 另一路 MCP authority 實作與測試；legacy `SQLiteStore` 不會自動啟用 scope/CAS/soft delete/authority lock |
| `recall-memory-mcp/` | 已有獨立 alpha distribution；保留既有來源，與 Hermes legacy adapter 不同 |
| `recall-core/`, `recall-server/`, mobile、extension、sync daemon | 不在本交付範圍；不宣稱此 wheel 已提供跨裝置同步或全客戶端單一權威 |

[`SPEC.md`](SPEC.md) 是設計規範，不是 legacy runtime 的功能清單。Hermes 安裝與回切見 [adapter README](https://github.com/Jnocode/recall-memory-hermes)；詳細保存策略見 [`docs/preservation.md`](docs/preservation.md)。Deprecated `recall.recall_mcp` 不作為本輪建議安裝面；新的 MCP alpha 指引位於 [`recall-memory-mcp/README.md`](recall-memory-mcp/README.md)。

## 保存與恢復

保留原 DB、一致性 SQLite backup、core/adapter 配對 wheel、checksum 與版本紀錄；**不要把私人 DB、對話、設定憑證上傳 GitHub**。資料庫使用 WAL，運行中不能只複製 `.db` 而忽略已提交 WAL；使用 SQLite backup API，或停止所有 Recall writer 後再做完整備份。

回切到 Recall 時，先在 DB 備份的副本上安裝上述配對 wheel、執行 `stats/query`，再依 adapter 文件選定 `memory.provider: recall-memory-hermes`、設定原庫位置並啟動新 session。切換 provider 不會自動匯入另一系統的新記憶，也不要刪除另一 provider 的資料。

## 開發與打包

```bash
python -m pip install -e ".[dev]" build twine
python scripts/version_sync_check.py
# test_p0_improvements 依賴未納入交付的 recall-server 雛形，刻意排除。
python -m pytest -q tests --ignore=tests/test_p0_improvements.py
python -m build --outdir ci-dist/core
python scripts/verify_distributions.py ci-dist/core
python -m twine check ci-dist/core/*
```

建置產物放 CI artifacts，不提交 `dist/` 或 egg-info。CI 在 master、`preserve/**` push、PR 或手動 dispatch 執行；`publish.yml` 的 `v*` tag 會發布 PyPI，**未獨立授權前不要建立／推送 tag**。

Apache-2.0，見 [LICENSE](LICENSE)。
