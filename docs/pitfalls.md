# recall. 🧠 踩坑記錄

> 開發 recall-memory 過程中遇到的坑與解法。
> 納入 wiki 知識庫作為系統性參考。

---

## Honcho API 端點

**坑：** 查 Honcho 用 `/api/` 舊端點拿到 404，錯誤判定 Honcho 為空。

**解：** Honcho v3 API 路徑為 `/v3/workspaces/`，需先查 DB 確認：
```bash
docker exec honcho-db psql -U honcho -d honcho -c "SELECT id, name FROM workspaces;"
docker exec honcho-db psql -U honcho -d honcho -c "SELECT count(*) FROM messages;"
```
Honcho 沒有直接 create document 的端點，資料透過 sessions/messages 寫入。

**教訓：** 永遠先確認 API 版本和端點列表（`/openapi.json`），不要只試一條路徑就下結論。

---

## SQLite nested quoting 在 execute_code

**坑：** `execute_code` 中的 `terminal(command='python3 -c "..."')` 會因為巢狀引號噴 SyntaxError。

**解：** 拆成多個獨立的 `terminal()` 呼叫，不要用巢狀 shell。

---

## sqlite-vec module not found

**坑：** 直接 `sqlite3.connect(recall_p0.db)` 會報 `no such module: vec0`，因為 sqlite-vec 是載入式 extension。

**解：** 透過 recall 的 CLI 或 API 操作 DB，不直接連。
```python
# 正確做法
from recall.store import SQLiteStore
store = SQLiteStore("path/to/recall_p0.db")
store.add(memory)

# 錯誤做法
conn = sqlite3.connect("recall_p0.db")  # ← vec0 module not loaded
```

---

## Hermes provider plugin 不存在

**坑：** 假設 Honcho 有 memory provider plugin 可以「修 connector」，但 memory tool 從未有過 Honcho connector。

**解：** 從零建立 provider plugin。Hermes plugin 路徑為 `plugins/memory/<name>/`，需實作 `MemoryProvider` abstract class（來自 `agent/memory_provider.py`）。

---

## Wiki pages API 唯讀

**坑：** Wiki API 只支援 GET，無法透過 API 建立新頁面。

**解：** 直接 clone wiki repo（github.com/Jnocode/wiki），在 `entities/` 或 `concepts/` 目錄下新增 markdown 檔案後推送。
