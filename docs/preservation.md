# Recall 保存交付與恢復契約

## 版本與來源

這輪 core 0.2.1 保存 fetched default branch 的 legacy 核心，adapter 0.3.1 延續其 fetched 0.3.0。Core 不覆寫成舊 installed runtime；保留來源已有的 sqlite-vec KNN constraint、session filter、score 與 cold promotion 修正。

相容的 adapter dependency range 為 `recall-sqlite>=0.2.0,<0.3`；它不使用新增 session filter，仍支援既有 0.2.0。**精確恢復本候選**必須安裝保存的 core 0.2.1 與 adapter 0.3.1 wheel，不能靠 range 解析到舊 index 版本。

## 不應發布的東西

私人 Recall DB（含 WAL/SHM）、內建使用者／記憶檔、完整 transcript、token、auth、runtime config、host-specific audit、臨時 logs/backups 與未完成 mobile/server/sync 原型不屬於 GitHub 程式交付。測試只使用 tmp fixture 與 demo 字串。Generated wheel/sdist 是可驗證的 CI artifact，不是 Git source。

## 回切前的資料保存

1. 記錄原庫位置、core/adapter 精確版本與 wheel checksum，保留舊來源和 artifacts。
2. 運行中用 SQLite backup API 取得一致性備份，或停止所有寫入後備份 DB 與需要的 WAL；不要對唯一原庫做 schema/cleanup 實驗。
3. 在備份副本上先用 explicit `RECALL_DB_PATH` 跑 `recall stats --verbose`、`recall query`，檢查既有資料能讀；必要時僅在副本上檢查 integrity。
4. 裝入配對 wheel（同一 Hermes interpreter／approved dependency target），再選 provider；active profile 的設定由 Hermes 管理。
5. 開新 session，驗證 provider status 和一條 synthetic durable 記憶的跨 session 召回；status available 不能代替 retrieval 證據。
6. 保留另一 provider 的資料與設定；期間新增的記憶不會自動出現在舊 Recall，另行核對／遷移。

## 已知語意

Legacy store 的 `delete` 是 hard delete，並未使用 MCP soft delete；project filter、session filter 與 MCP scope 授權是不同層。Adapter 0.3.x 的 built-in replace 是 add-before-delete，但不是跨兩次寫入的原子交易；delete 失敗可能短暫留兩筆。舊 deployed plugin 的 delete-old-first 行為較弱，不作為新候選來源。

Core 啟動可能補 legacy tier columns；不會自動啟用 MCP authority migration。不要把 SPEC、alpha MCP 測試或安裝成功解讀成全客戶端 production acceptance。
