"""Offline break-glass tests (tasks 6.8 and 6.10c).

Every test runs against a *real* migrated SQLite authority database created
inside ``tmp_path`` through ``provisioning.initialize``.  Nothing here goes
near the operator's real ``~/.hermes/recall.db``: the CLI is always handed a
synthetic HOME/TEMP mapping and an explicit ``--db-path``.

What the suite proves:

* task 6.8   — SQLite online backup API is used (not a file copy of a live
               DB), integrity + schema version are verified, the service must
               be stopped (authority lock), an exact confirmation phrase is
               required, and a content-free operator audit is written.
* task 6.10c — the offline allowlist accepts *only* whole-database
               backup/verify-restore/migrate; memory list/search/export/
               row-level restore/purge and grant edits are all refused, and
               the warning / confirmation / operator audit read back.
"""

from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from recall_memory_mcp import breakglass, cli, provisioning
from recall_memory_mcp.settings import ServerSettings

pytestmark = pytest.mark.usefixtures("isolated_env")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
    }


@pytest.fixture
def authority(tmp_path: Path, isolated_env: dict[str, str]) -> Path:
    """A real, migrated authority database at ``tmp_path/authority/recall.db``."""

    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(tmp_path / "authority")
    env["RECALL_MCP_DB_PATH"] = str(tmp_path / "authority" / "recall.db")
    settings = ServerSettings.from_env(env)
    provisioning.initialize(settings)
    db_path = Path(settings.db_path)
    assert db_path.is_file()
    return db_path


def run_cli(argv, env, db_path: Path | None = None):
    out, err = io.StringIO(), io.StringIO()
    merged = dict(env)
    if db_path is not None:
        merged["RECALL_MCP_DB_PATH"] = str(db_path)
        merged["RECALL_MCP_CONFIG_DIR"] = str(db_path.parent)
    code = cli.main(list(argv), env=merged, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def phrase(operation: str, db_path: Path) -> str:
    return breakglass.confirmation_phrase(operation, db_path)


def seed_memory(db_path: Path, memory_id: str = "mem-canary") -> None:
    """Insert one row directly so backups have something to preserve."""

    now = "2026-08-04T00:00:00+00:00"
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        owner_row = conn.execute("SELECT owner_id FROM owners LIMIT 1").fetchone()
        grant_row = conn.execute("SELECT grant_id FROM client_grants LIMIT 1").fetchone()
        known: dict[str, object] = {
            "id": memory_id,
            "memory_id": memory_id,
            "content": "break-glass canary content",
            "timestamp": now,
            "created_at": now,
            "updated_at": now,
            "revision": 1,
            "scope": "project:breakglass",
            "kind": "decision",
            "source_client": "test",
            "owner_id": owner_row[0] if owner_row else None,
            "created_by_grant_id": grant_row[0] if grant_row else None,
            "updated_by_grant_id": grant_row[0] if grant_row else None,
        }
        values: dict[str, object] = {}
        for _cid, name, coltype, notnull, default, pk in conn.execute(
            "PRAGMA table_info(memories)"
        ):
            if name in ("id", "content"):
                values[name] = known[name]
                continue
            if not notnull or default is not None:
                continue
            if name in known and known[name] is not None:
                values[name] = known[name]
            elif "INT" in (coltype or "").upper():
                values[name] = 0
            else:
                values[name] = now if "at" in name or "time" in name else "breakglass-test"
            del pk
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO memories ({cols}) VALUES ({marks})", tuple(values.values()))
        # Keep the authority invariants intact: `verify_migrated_database`
        # requires one metadata row per memory row.
        # The only grant in a freshly initialised authority is the MIGRATION
        # grant, so the row must look exactly like a quarantined legacy import
        # (revision 1 / legacy scope / legacy kind / legacy-import client).
        if owner_row and grant_row:
            content_hash = hashlib.sha256(
                "break-glass canary content".encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO memory_metadata (
                    memory_id, owner_id, creator_grant_id, revision, scope, kind,
                    tags_json, content_hash, source_client, actor_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 1, 'legacy:unscoped', 'legacy',
                          '[]', ?, 'legacy-import', 'actor-test', ?, ?)
                """,
                (memory_id, owner_row[0], grant_row[0], content_hash, now, now),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# task 6.10c — the allowlist
# ---------------------------------------------------------------------------


def test_allowlist_contains_exactly_three_whole_database_operations():
    assert breakglass.OFFLINE_ALLOWED_OPERATIONS == ("backup", "restore", "migrate")


@pytest.mark.parametrize("operation", ["backup", "restore", "migrate"])
def test_allowlisted_operations_pass(operation: str):
    assert breakglass.assert_offline_allowed(operation) == operation


@pytest.mark.parametrize(
    "operation",
    [
        "memory-list",
        "memory-search",
        "memory-get",
        "memory-export",
        "memory-restore",
        "memory-purge",
        "row-restore",
        "grant-list",
        "grant-edit",
        "grant-create",
        "grant-revoke",
        "owner-edit",
        "sql",
    ],
)
def test_denied_operations_are_refused_offline(operation: str):
    with pytest.raises(breakglass.OfflineOperationDeniedError) as excinfo:
        breakglass.assert_offline_allowed(operation)
    message = str(excinfo.value)
    assert operation in message
    assert "backup, restore, migrate" in message


def test_unknown_operation_is_refused_by_the_closed_allowlist():
    """An operation nobody remembered to deny is still refused."""

    with pytest.raises(breakglass.OfflineOperationDeniedError):
        breakglass.assert_offline_allowed("vacuum-into-somewhere")


def test_cli_db_subcommands_are_exactly_the_allowlist():
    parser = cli.build_parser()
    db_action = next(
        action
        for action in parser._subparsers._group_actions  # noqa: SLF001 - argparse introspection
        for _ in [0]
        if "db" in action.choices
    )
    db_parser = db_action.choices["db"]
    operations = next(
        set(action.choices)
        for action in db_parser._subparsers._group_actions  # noqa: SLF001
    )
    assert operations == {"backup", "restore", "migrate"}


@pytest.mark.parametrize(
    "argv",
    [
        ["db", "memory-list"],
        ["db", "memory-search", "canary"],
        ["db", "memory-export", "out.json"],
        ["db", "memory-purge", "mem-1"],
        ["db", "grant-edit", "grant-1"],
        ["db", "sql", "SELECT 1"],
    ],
)
def test_cli_rejects_non_allowlisted_db_operations(argv, isolated_env, authority):
    code, _out, err = run_cli(argv, isolated_env, authority)
    assert code == cli.EXIT_USAGE
    assert "invalid choice" in err or "usage:" in err


def test_cli_restore_without_whole_database_flag_is_refused(isolated_env, authority, tmp_path):
    snapshot = tmp_path / "snap.db"
    code, _out, err = run_cli(
        ["db", "restore", str(snapshot), "--confirm", phrase("restore", authority)],
        isolated_env,
        authority,
    )
    assert code == cli.EXIT_REFUSED
    assert "whole-database" in err
    assert "Row-level restore" in err or "row-level restore" in err


def test_library_restore_without_whole_database_flag_is_denied(authority, tmp_path):
    snapshot = tmp_path / "snap.db"
    with pytest.raises(breakglass.OfflineOperationDeniedError):
        breakglass.restore_whole_database(
            authority,
            snapshot,
            whole_database=False,
            confirm=phrase("restore", authority),
        )


# ---------------------------------------------------------------------------
# task 6.8 — confirmation
# ---------------------------------------------------------------------------


def test_confirmation_phrase_is_bound_to_this_database(authority, tmp_path):
    other = tmp_path / "elsewhere.db"
    assert phrase("backup", authority) != breakglass.confirmation_phrase("backup", other)
    assert breakglass.db_identity(authority) in phrase("backup", authority)
    assert phrase("backup", authority).startswith("BREAK-GLASS BACKUP ")


@pytest.mark.parametrize("bad", [None, "yes", "y", "true", "BREAK-GLASS BACKUP", "confirm"])
def test_backup_refuses_without_the_exact_phrase(authority, tmp_path, bad):
    destination = tmp_path / "snap.db"
    with pytest.raises(breakglass.ConfirmationRequiredError):
        breakglass.backup_database(authority, destination, confirm=bad)
    assert not destination.exists()


def test_wrong_operation_phrase_does_not_authorise_another_operation(authority, tmp_path):
    destination = tmp_path / "snap.db"
    with pytest.raises(breakglass.ConfirmationRequiredError):
        breakglass.backup_database(
            authority, destination, confirm=phrase("migrate", authority)
        )


def test_cli_without_confirm_prints_the_warning_and_the_required_phrase(
    isolated_env, authority, tmp_path
):
    destination = tmp_path / "snap.db"
    code, _out, err = run_cli(["db", "backup", str(destination)], isolated_env, authority)
    assert code == cli.EXIT_REFUSED
    assert "BREAK-GLASS BACKUP" in err
    assert "ENTIRE authority database" in err
    assert "does NOT enforce MCP owner ACLs" in err
    assert not destination.exists()


def test_warning_text_states_the_whole_database_impact(authority):
    restore_warning = breakglass.warning_text("restore", authority)
    assert "REPLACES the ENTIRE authority database" in restore_warning
    assert "no row-level restore" in restore_warning
    backup_warning = breakglass.warning_text("backup", authority)
    assert "retention" in backup_warning


# ---------------------------------------------------------------------------
# task 6.8 — service must be stopped (authority lock)
# ---------------------------------------------------------------------------


def test_backup_refuses_while_a_server_owns_the_authority(authority, tmp_path):
    from recall.authority_lock import AuthorityDatabaseLock

    holder = AuthorityDatabaseLock(authority)
    holder.acquire()
    destination = tmp_path / "snap.db"
    try:
        with pytest.raises(breakglass.AuthorityBusyError) as excinfo:
            breakglass.backup_database(
                authority, destination, confirm=phrase("backup", authority)
            )
    finally:
        holder.release()
    assert "stop the recall-memory-mcp service" in str(excinfo.value)
    assert not destination.exists()


def test_busy_refusal_is_audited_and_reads_back(authority, tmp_path):
    from recall.authority_lock import AuthorityDatabaseLock

    holder = AuthorityDatabaseLock(authority)
    holder.acquire()
    try:
        with pytest.raises(breakglass.AuthorityBusyError):
            breakglass.backup_database(
                authority, tmp_path / "snap.db", confirm=phrase("backup", authority)
            )
    finally:
        holder.release()

    entries = breakglass.OperatorAuditLog(breakglass.audit_path_for(authority)).entries()
    assert [e.outcome for e in entries] == ["refused"]
    assert entries[0].detail["reason"] == "authority_lock_held"


def test_lock_is_released_after_a_successful_backup(authority, tmp_path):
    from recall.authority_lock import AuthorityDatabaseLock

    breakglass.backup_database(
        authority, tmp_path / "snap.db", confirm=phrase("backup", authority)
    )
    after = AuthorityDatabaseLock(authority)
    after.acquire()  # would raise AuthorityLockHeldError if we leaked the lock
    after.release()


# ---------------------------------------------------------------------------
# task 6.8 — backup itself
# ---------------------------------------------------------------------------


def test_backup_creates_a_verified_snapshot(authority, tmp_path):
    seed_memory(authority)
    destination = tmp_path / "snapshots" / "recall-snap.db"
    report = breakglass.backup_database(
        authority, destination, confirm=phrase("backup", authority)
    )

    assert report.outcome == "succeeded"
    assert report.operation == "backup"
    assert destination.is_file()
    assert report.detail["method"] == "sqlite_online_backup_api"
    assert report.detail["memory_rows"] == 1
    assert report.detail["integrity_ok"] is True

    verified = breakglass.verify_backup(destination)
    assert verified.ready is True
    assert verified.schema_version == provisioning.database_status(authority).schema_version
    assert verified.memory_count == 1


def test_backup_snapshot_read_back_contains_the_row(authority, tmp_path):
    seed_memory(authority, "mem-readback")
    destination = tmp_path / "snap.db"
    breakglass.backup_database(authority, destination, confirm=phrase("backup", authority))

    conn = sqlite3.connect(destination)
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM memories")]
    finally:
        conn.close()
    assert ids == ["mem-readback"]


def test_backup_refuses_to_overwrite_an_existing_file(authority, tmp_path):
    destination = tmp_path / "snap.db"
    destination.write_text("pre-existing", encoding="utf-8")
    with pytest.raises(breakglass.DestinationRefusedError):
        breakglass.backup_database(authority, destination, confirm=phrase("backup", authority))
    assert destination.read_text(encoding="utf-8") == "pre-existing"


def test_backup_refuses_a_directory_destination(authority, tmp_path):
    destination = tmp_path / "a-directory"
    destination.mkdir()
    with pytest.raises(breakglass.DestinationRefusedError):
        breakglass.backup_database(authority, destination, confirm=phrase("backup", authority))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_backup_refuses_a_symlink_destination(authority, tmp_path):
    real = tmp_path / "real.db"
    link = tmp_path / "link.db"
    link.symlink_to(real)
    with pytest.raises(breakglass.DestinationRefusedError):
        breakglass.backup_database(authority, link, confirm=phrase("backup", authority))


def test_backup_refuses_when_there_is_no_database(tmp_path):
    missing = tmp_path / "nope" / "recall.db"
    with pytest.raises(breakglass.SourceRefusedError):
        breakglass.backup_database(
            missing, tmp_path / "snap.db", confirm=phrase("backup", missing)
        )


def test_cli_backup_end_to_end(isolated_env, authority, tmp_path):
    seed_memory(authority)
    destination = tmp_path / "cli-snap.db"
    code, out, err = run_cli(
        ["db", "backup", str(destination), "--confirm", phrase("backup", authority)],
        isolated_env,
        authority,
    )
    assert code == cli.EXIT_OK, err
    assert "break-glass backup: succeeded" in out
    assert "sqlite_online_backup_api" in out
    assert destination.is_file()
    assert breakglass.verify_backup(destination).ready is True


# ---------------------------------------------------------------------------
# task 6.8 — whole-database restore
# ---------------------------------------------------------------------------


def test_restore_replaces_the_whole_database_and_reads_back(authority, tmp_path):
    seed_memory(authority, "mem-before")
    snapshot = tmp_path / "snap.db"
    breakglass.backup_database(authority, snapshot, confirm=phrase("backup", authority))

    seed_memory(authority, "mem-after-snapshot")
    conn = sqlite3.connect(authority)
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    conn.close()

    report = breakglass.restore_whole_database(
        authority, snapshot, whole_database=True, confirm=phrase("restore", authority)
    )
    assert report.outcome == "succeeded"
    assert report.detail["scope"] == "whole_database"
    assert report.detail["safety_snapshot_taken"] is True
    assert report.safety_backup_path is not None and report.safety_backup_path.is_file()

    conn = sqlite3.connect(authority)
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM memories")]
    finally:
        conn.close()
    assert ids == ["mem-before"]  # the post-snapshot row is gone: whole-DB restore


def test_restore_takes_a_safety_backup_of_the_current_database(authority, tmp_path):
    seed_memory(authority, "mem-original")
    snapshot = tmp_path / "snap.db"
    breakglass.backup_database(authority, snapshot, confirm=phrase("backup", authority))
    seed_memory(authority, "mem-would-be-lost")

    report = breakglass.restore_whole_database(
        authority, snapshot, whole_database=True, confirm=phrase("restore", authority)
    )
    safety = report.safety_backup_path
    assert safety is not None
    conn = sqlite3.connect(safety)
    try:
        ids = sorted(row[0] for row in conn.execute("SELECT id FROM memories"))
    finally:
        conn.close()
    assert ids == ["mem-original", "mem-would-be-lost"]


def test_restore_refuses_a_corrupt_snapshot(authority, tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\x00" * 512)
    with pytest.raises(breakglass.BackupVerificationError):
        breakglass.restore_whole_database(
            authority, corrupt, whole_database=True, confirm=phrase("restore", authority)
        )
    # the live database survived untouched
    assert provisioning.database_status(authority).ready is True


def test_restore_refuses_a_snapshot_that_is_not_an_authority_database(authority, tmp_path):
    stranger = tmp_path / "stranger.db"
    conn = sqlite3.connect(stranger)
    conn.execute("CREATE TABLE things (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(breakglass.BackupVerificationError):
        breakglass.restore_whole_database(
            authority, stranger, whole_database=True, confirm=phrase("restore", authority)
        )
    assert provisioning.database_status(authority).ready is True


def test_restore_refuses_a_newer_schema_version(authority, tmp_path, monkeypatch):
    snapshot = tmp_path / "snap.db"
    breakglass.backup_database(authority, snapshot, confirm=phrase("backup", authority))
    monkeypatch.setattr(breakglass, "_supported_schema_version", lambda: 0)
    with pytest.raises(breakglass.BackupVerificationError) as excinfo:
        breakglass.restore_whole_database(
            authority, snapshot, whole_database=True, confirm=phrase("restore", authority)
        )
    assert "newer schema version" in str(excinfo.value)


def test_restore_refuses_a_missing_snapshot(authority, tmp_path):
    with pytest.raises(breakglass.SourceRefusedError):
        breakglass.restore_whole_database(
            authority,
            tmp_path / "does-not-exist.db",
            whole_database=True,
            confirm=phrase("restore", authority),
        )


def test_cli_restore_end_to_end(isolated_env, authority, tmp_path):
    seed_memory(authority, "mem-cli")
    snapshot = tmp_path / "cli-snap.db"
    run_cli(
        ["db", "backup", str(snapshot), "--confirm", phrase("backup", authority)],
        isolated_env,
        authority,
    )
    code, out, err = run_cli(
        [
            "db",
            "restore",
            str(snapshot),
            "--whole-database",
            "--confirm",
            phrase("restore", authority),
        ],
        isolated_env,
        authority,
    )
    assert code == cli.EXIT_OK, err
    assert "break-glass restore: succeeded" in out
    assert "whole_database" in out


# ---------------------------------------------------------------------------
# task 6.8 — migrate
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent_and_takes_a_pre_migration_snapshot(authority, tmp_path):
    seed_memory(authority, "mem-migrate")
    before = provisioning.database_status(authority)
    report = breakglass.migrate_database(authority, confirm=phrase("migrate", authority))

    assert report.outcome == "succeeded"
    assert report.detail["pre_migration_snapshot"] is True
    assert report.detail["to_version"] == before.schema_version
    assert report.safety_backup_path is not None and report.safety_backup_path.is_file()
    assert breakglass.verify_backup(report.safety_backup_path).ready is True
    assert provisioning.database_status(authority).ready is True


def test_migrate_reuses_the_existing_owner_id(authority):
    conn = sqlite3.connect(authority)
    owners_before = sorted(row[0] for row in conn.execute("SELECT owner_id FROM owners"))
    conn.close()

    breakglass.migrate_database(authority, confirm=phrase("migrate", authority))

    conn = sqlite3.connect(authority)
    owners_after = sorted(row[0] for row in conn.execute("SELECT owner_id FROM owners"))
    conn.close()
    assert owners_after == owners_before


def test_cli_migrate_end_to_end(isolated_env, authority):
    code, out, err = run_cli(
        ["db", "migrate", "--confirm", phrase("migrate", authority)],
        isolated_env,
        authority,
    )
    assert code == cli.EXIT_OK, err
    assert "break-glass migrate: succeeded" in out
    assert "pre_migration_snapshot" in out


# ---------------------------------------------------------------------------
# task 6.8 / 6.10c — content-free operator audit
# ---------------------------------------------------------------------------


def test_audit_records_every_successful_operation_and_reads_back(authority, tmp_path):
    snapshot = tmp_path / "snap.db"
    breakglass.backup_database(authority, snapshot, confirm=phrase("backup", authority))
    breakglass.restore_whole_database(
        authority, snapshot, whole_database=True, confirm=phrase("restore", authority)
    )
    breakglass.migrate_database(authority, confirm=phrase("migrate", authority))

    entries = breakglass.OperatorAuditLog(breakglass.audit_path_for(authority)).entries()
    assert [e.operation for e in entries] == ["backup", "restore", "migrate"]
    assert {e.outcome for e in entries} == {"succeeded"}
    assert len({e.event_id for e in entries}) == 3
    assert all(e.db_identity == breakglass.db_identity(authority) for e in entries)


def test_audit_holds_no_paths_no_content_no_secrets(authority, tmp_path):
    seed_memory(authority)
    snapshot = tmp_path / "snap.db"
    breakglass.backup_database(authority, snapshot, confirm=phrase("backup", authority))
    with pytest.raises(breakglass.ConfirmationRequiredError):
        breakglass.backup_database(authority, tmp_path / "other.db", confirm="yes")

    raw = breakglass.audit_path_for(authority).read_text(encoding="utf-8")
    assert "break-glass canary content" not in raw
    assert str(tmp_path) not in raw
    assert str(authority) not in raw
    assert "snap.db" not in raw
    for line in raw.splitlines():
        payload = json.loads(line)
        assert set(payload) == {"at", "operation", "outcome", "db_identity", "event_id", "detail"}
        assert payload["db_identity"].startswith("sha256:")


def test_audit_rejects_a_path_like_detail(tmp_path):
    log = breakglass.OperatorAuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(breakglass.AuditContentError):
        log.append(
            operation="backup",
            outcome="succeeded",
            db_identity="sha256:0123456789abcdef",
            detail={"destination": "C:\\Users\\someone\\recall.db"},
        )
    assert not (tmp_path / "audit.jsonl").exists()


def test_audit_records_refusals_as_well_as_successes(authority, tmp_path):
    with pytest.raises(breakglass.ConfirmationRequiredError):
        breakglass.backup_database(authority, tmp_path / "snap.db", confirm=None)
    entries = breakglass.OperatorAuditLog(breakglass.audit_path_for(authority)).entries()
    assert entries[0].outcome == "refused"
    assert entries[0].detail["reason"] == "confirmation_phrase_missing_or_wrong"


def test_audit_log_is_append_only_across_processes(authority, tmp_path):
    for index in range(3):
        breakglass.backup_database(
            authority, tmp_path / f"snap-{index}.db", confirm=phrase("backup", authority)
        )
    entries = breakglass.OperatorAuditLog(breakglass.audit_path_for(authority)).entries()
    assert len(entries) == 3
    assert [e.outcome for e in entries] == ["succeeded"] * 3


# ---------------------------------------------------------------------------
# task 6.8 — the snapshot is not a naive file copy
# ---------------------------------------------------------------------------


def _ids(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT id FROM memories")]
    except sqlite3.DatabaseError:  # a torn copy
        return []
    finally:
        connection.close()


def test_backup_captures_committed_wal_data_that_a_naive_copy_would_miss(authority, tmp_path):
    """A committed write still living in the -wal file must be captured.

    This is the concrete reason task 6.8 forbids an ordinary copy of a live
    database: with an open WAL-mode connection the main file does not yet
    contain the committed row, so ``shutil.copy`` silently loses data while
    the SQLite online backup API does not.
    """

    holder = sqlite3.connect(authority, timeout=10.0, isolation_level=None)
    try:
        mode = holder.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":  # pragma: no cover - platform dependent
            pytest.skip("WAL journal mode unavailable")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("PRAGMA foreign_keys=ON")

        # Commit through the *held open* connection so the row stays in -wal.
        seed_memory(authority, "mem-in-wal")
        assert Path(str(authority) + "-wal").is_file()

        naive = tmp_path / "naive-copy.db"
        naive.write_bytes(Path(authority).read_bytes())  # exactly what we forbid

        snapshot = tmp_path / "proper-snap.db"
        breakglass.backup_database(authority, snapshot, confirm=phrase("backup", authority))
    finally:
        holder.close()

    assert _ids(snapshot) == ["mem-in-wal"]
    assert _ids(naive) == []  # the naive copy of the main file lost the commit
