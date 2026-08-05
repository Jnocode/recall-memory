"""Owner-scoped admin service tests (tasks 6.10 / 6.10a / 6.10b).

Two halves:

* :class:`TestAdminServiceContract` drives :class:`AdminService` against a
  recording fake so the *decisions* (who may call, what is digested, what is
  audited) are visible in isolation.
* Everything below ``# real authority`` builds a **real** SQLite authority in
  ``tmp_path``, performs a real add/remove/restore/purge cycle through the
  real core repository, and then proves the purge is irreversible by scanning
  the database, replaying the old idempotency key and reading the file back
  from a fresh connection.

Nothing here may touch the operator's private database: every settings object
is built from an explicit synthetic environment rooted at ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from recall_memory_mcp import admin, authority, models, provisioning
from recall_memory_mcp import repository as repo
from recall_memory_mcp.service import CallerContext
from recall_memory_mcp.settings import ServerSettings

pytest.importorskip("recall.mcp_repository", reason="the canonical recall-sqlite core is required")

SCOPE = "global"
OTHER_SCOPE = "project:other"
CANARY = "Ratatoskr-canary-9f3a1c-do-not-index-elsewhere"
DIMENSION = 8

STUB_IDENTITY = authority.EmbeddingIdentity(
    provider="test-provider",
    model="stub-embed-8",
    endpoint_identity_hash="0" * 64,
)


class StubEmbedder:
    def __init__(self, *, available: bool = True, dimension: int = DIMENSION) -> None:
        self.available = available
        self.dimension = dimension
        self.identity = STUB_IDENTITY

    def encode(self, text: str) -> dict[int, bytes]:
        if not self.available:
            raise repo.EmbeddingUnavailableError("provider down")
        seed = abs(hash(text)) % 997 or 1
        vector = [((seed * (i + 1)) % 101) / 100.0 for i in range(self.dimension)]
        return {authority.DEFAULT_GENERATION: authority.pack_vector(vector)}


# ---------------------------------------------------------------------------
# fake repository half
# ---------------------------------------------------------------------------


class FakeOutcome:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeAdminRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: list[dict[str, Any]] = []
        self.raises: dict[str, Exception] = {}

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.raises:
            raise self.raises[name]

    def export_scoped(self, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        self._record("export_scoped", kwargs)
        if kwargs.get("include_tombstones"):
            return tuple(self.rows)
        return tuple(row for row in self.rows if row.get("deleted_at") is None)

    def restore_memory(self, **kwargs: Any) -> FakeOutcome:
        self._record("restore_memory", kwargs)
        return FakeOutcome(
            memory_id=kwargs["memory_id"],
            revision=kwargs["expected_revision"] + 1,
            restored_at="2026-08-05T00:00:00+00:00",
        )

    def purge_memory(self, **kwargs: Any) -> FakeOutcome:
        self._record("purge_memory", kwargs)
        return FakeOutcome(
            memory_id=kwargs["memory_id"],
            final_revision=kwargs["expected_revision"] + 1,
            purged_at="2026-08-05T00:00:00+00:00",
        )

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def make_row(memory_id: str = "mem-1", *, deleted_at: str | None = None) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "content": CANARY,
        "revision": 1,
        "scope": SCOPE,
        "kind": "note",
        "tags": ("canary",),
        "content_hash": "sha256:deadbeef",
        "source_client": "local-operator",
        "source_conversation": None,
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "deleted_at": deleted_at,
    }


def make_ctx(
    *,
    oauth_scopes: tuple[str, ...] = ("memory:read", "memory:write", "memory:admin"),
    memory_scopes: tuple[str, ...] = (SCOPE,),
) -> CallerContext:
    return CallerContext(
        grant_id="grant-local-owner-1",
        owner_id="owner-local",
        actor_id="actor:local-operator",
        source_client="local-operator",
        oauth_scopes=oauth_scopes,
        memory_scopes=memory_scopes,
    )


def make_admin(tmp_path: Path, repository: FakeAdminRepository) -> admin.AdminService:
    return admin.AdminService(
        repository=repository,
        digest_key=b"deterministic-admin-digest-key",
        db_identity="sha256:" + "0" * 64,
        audit=admin.AdminAuditLog(tmp_path / "admin-audit.jsonl"),
        clock=lambda: "2026-08-05T00:00:00+00:00",
    )


class TestAdminServiceContract:
    def test_export_requires_memory_admin(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        service = make_admin(tmp_path, repository)
        ctx = make_ctx(oauth_scopes=("memory:read", "memory:write"))
        with pytest.raises(admin.AdminNotAuthorizedError):
            service.export(ctx, scope=SCOPE)
        assert repository.names() == [], "an unauthorized export must not reach storage"

    def test_export_requires_exact_scope(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        service = make_admin(tmp_path, repository)
        with pytest.raises(admin.AdminNotAuthorizedError):
            service.export(make_ctx(), scope=OTHER_SCOPE)
        assert repository.names() == []

    def test_refusal_message_is_uniform(self, tmp_path: Path) -> None:
        service = make_admin(tmp_path, FakeAdminRepository())
        with pytest.raises(admin.AdminNotAuthorizedError) as no_scope:
            service.export(make_ctx(), scope=OTHER_SCOPE)
        with pytest.raises(admin.AdminNotAuthorizedError) as no_admin:
            service.export(make_ctx(oauth_scopes=("memory:read",)), scope=SCOPE)
        assert str(no_scope.value) == str(no_admin.value) == "not authorized"

    def test_export_excludes_tombstones_by_default(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.rows = [make_row("mem-live"), make_row("mem-dead", deleted_at="2026-08-05T00:00:00+00:00")]
        service = make_admin(tmp_path, repository)
        result = service.export(make_ctx(), scope=SCOPE)
        assert [m.memory_id for m in result.memories] == ["mem-live"]
        document = result.document()
        assert document["includes_tombstones"] is False
        assert document["memory_count"] == 1
        assert document["tombstone_count"] == 0
        assert repository.calls[0][1]["include_tombstones"] is False

    def test_export_includes_tombstones_on_request_and_says_so(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.rows = [make_row("mem-live"), make_row("mem-dead", deleted_at="2026-08-05T00:00:00+00:00")]
        service = make_admin(tmp_path, repository)
        result = service.export(make_ctx(), scope=SCOPE, include_tombstones=True)
        document = result.document()
        assert document["includes_tombstones"] is True
        assert document["memory_count"] == 2
        assert document["tombstone_count"] == 1
        assert admin.BACKUP_RETENTION_NOTICE in document["backup_retention_notice"]

    def test_export_never_names_an_owner(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.rows = [make_row()]
        service = make_admin(tmp_path, repository)
        result = service.export(make_ctx(), scope=SCOPE)
        assert "owner_id" not in json.dumps(result.document())
        assert "owner_id" not in repository.calls[0][1]

    def test_purge_deny_digest_is_keyed_and_distinct(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        service = make_admin(tmp_path, repository)
        service.purge(
            make_ctx(), memory_id="mem-1", scope=SCOPE, expected_revision=2, idempotency_key="key-0001"
        )
        kwargs = repository.calls[0][1]
        assert kwargs["payload_digest"] != kwargs["deny_payload_digest"]

        other = admin.AdminService(
            repository=FakeAdminRepository(),
            digest_key=b"a-different-key",
            db_identity="sha256:" + "0" * 64,
            audit=None,
            clock=lambda: "2026-08-05T00:00:00+00:00",
        )
        other.purge(
            make_ctx(), memory_id="mem-1", scope=SCOPE, expected_revision=2, idempotency_key="key-0001"
        )
        assert other.repository.calls[0][1]["deny_payload_digest"] != kwargs["deny_payload_digest"]

    def test_purge_digests_never_contain_content(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        service = make_admin(tmp_path, repository)
        service.purge(
            make_ctx(), memory_id="mem-1", scope=SCOPE, expected_revision=1, idempotency_key="key-0009"
        )
        blob = json.dumps(repository.calls[0][1])
        assert CANARY not in blob

    def test_revision_conflict_is_surfaced_with_current_revision(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.raises["purge_memory"] = repo.RevisionConflictError(current_revision=7)
        service = make_admin(tmp_path, repository)
        with pytest.raises(admin.AdminRevisionConflictError) as caught:
            service.purge(
                make_ctx(), memory_id="m", scope=SCOPE, expected_revision=1, idempotency_key="key-0009"
            )
        assert caught.value.current_revision == 7

    def test_repository_permission_error_becomes_uniform_refusal(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.raises["restore_memory"] = PermissionError("not authorized")
        service = make_admin(tmp_path, repository)
        with pytest.raises(admin.AdminNotAuthorizedError):
            service.restore(
                make_ctx(), memory_id="m", scope=SCOPE, expected_revision=1, idempotency_key="key-0009"
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"memory_id": "", "scope": SCOPE, "expected_revision": 1, "idempotency_key": "k"},
            {"memory_id": "m", "scope": " ", "expected_revision": 1, "idempotency_key": "k"},
            {"memory_id": "m", "scope": SCOPE, "expected_revision": 0, "idempotency_key": "k"},
            {"memory_id": "m", "scope": SCOPE, "expected_revision": True, "idempotency_key": "k"},
            {"memory_id": "m", "scope": SCOPE, "expected_revision": "2", "idempotency_key": "k"},
            {"memory_id": "m", "scope": SCOPE, "expected_revision": 1, "idempotency_key": ""},
        ],
    )
    def test_boundary_arguments_are_refused_before_storage(
        self, tmp_path: Path, kwargs: dict[str, Any]
    ) -> None:
        repository = FakeAdminRepository()
        service = make_admin(tmp_path, repository)
        with pytest.raises(admin.AdminError):
            service.purge(make_ctx(), **kwargs)
        assert repository.names() == []

    def test_audit_is_content_free_and_keyed(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.rows = [make_row()]
        service = make_admin(tmp_path, repository)
        service.export(make_ctx(), scope=SCOPE)
        service.purge(
            make_ctx(), memory_id="mem-1", scope=SCOPE, expected_revision=1, idempotency_key="key-0009"
        )
        raw = (tmp_path / "admin-audit.jsonl").read_text(encoding="utf-8")
        assert CANARY not in raw
        assert "owner-local" not in raw, "the raw owner id must not be written to the audit"
        assert "mem-1" not in raw, "the raw memory id must not be written to the audit"
        entries = service.audit.entries()
        assert [entry.operation for entry in entries] == ["memory_export", "memory_purge"]
        assert entries[1].detail["irreversible"] is True
        assert entries[1].detail["final_revision"] == 2

    def test_refused_operations_are_audited_too(self, tmp_path: Path) -> None:
        repository = FakeAdminRepository()
        repository.raises["purge_memory"] = PermissionError("not authorized")
        service = make_admin(tmp_path, repository)
        with pytest.raises(admin.AdminNotAuthorizedError):
            service.purge(
                make_ctx(), memory_id="m", scope=SCOPE, expected_revision=1, idempotency_key="key-0009"
            )
        entries = service.audit.entries()
        assert entries[-1].outcome == "refused"
        assert entries[-1].detail["reason"] == "AdminNotAuthorizedError"

    def test_audit_refuses_a_path_like_detail(self, tmp_path: Path) -> None:
        service = make_admin(tmp_path, FakeAdminRepository())
        with pytest.raises(admin.AdminUnavailableError):
            service._record(
                operation="memory_export", outcome="ok", detail={"where": "C:/secrets/db.sqlite"}
            )


# ---------------------------------------------------------------------------
# real authority
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: str) -> ServerSettings:
    env = {
        "RECALL_MCP_CONFIG_DIR": str(tmp_path / "authority"),
        "RECALL_MCP_MODE": "local",
        "RECALL_MCP_HOST": "127.0.0.1",
        "RECALL_MCP_PORT": "19882",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"),
        "TEMP": str(tmp_path / "temp"),
    }
    env.update(overrides)
    return ServerSettings.from_env(env)


def make_stack(
    tmp_path: Path, *, memory_scopes: tuple[str, ...] = (SCOPE,)
) -> tuple[ServerSettings, Any, admin.AdminService, authority.AuthorityGrant]:
    settings = make_settings(tmp_path)
    provisioning.initialize(settings)
    service = authority.build_service(
        settings,
        embedder=StubEmbedder(),
        env={},
        digest_key=b"deterministic-test-digest-key",
        clock=lambda: "2026-08-05T00:00:00+00:00",
    )
    grant = authority.ensure_local_grant(settings.db_path, memory_scopes=memory_scopes)
    admin_service = admin.build_admin_service(
        settings, repository=service.repository, digest_key=b"deterministic-test-digest-key"
    )
    return settings, service, admin_service, grant


def data_of(envelope: Any) -> dict[str, Any]:
    payload = envelope.model_dump(mode="json")
    assert "error" not in payload, f"unexpected error envelope: {payload}"
    return payload["data"]


def error_of(envelope: Any) -> dict[str, Any]:
    payload = envelope.model_dump(mode="json")
    assert "error" in payload, f"expected an error envelope, got: {payload}"
    return payload["error"]


def add(service: Any, ctx: Any, *, content: str, key: str, scope: str = SCOPE) -> dict[str, Any]:
    return data_of(
        service.add(
            ctx,
            models.AddRequest(
                content=content, scope=scope, kind="note", tags=("canary",), idempotency_key=key
            ),
        )
    )


def remove(service: Any, ctx: Any, *, memory_id: str, revision: int, key: str, scope: str = SCOPE) -> dict[str, Any]:
    return data_of(
        service.remove(
            ctx,
            models.RemoveRequest(
                memory_id=memory_id, scope=scope, expected_revision=revision, idempotency_key=key
            ),
        )
    )


def all_text_values(db_path: Path) -> list[str]:
    """Every text value in every table of the database (FTS shadows included)."""

    conn = sqlite3.connect(db_path)
    try:
        conn.text_factory = bytes
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table')"
            ).fetchall()
        ]
        found: list[str] = []
        for table in tables:
            name = table.decode("utf-8") if isinstance(table, bytes) else table
            try:
                rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
            except sqlite3.DatabaseError:
                continue  # shadow tables that refuse a direct SELECT
            for row in rows:
                for value in row:
                    if isinstance(value, bytes):
                        found.append(value.decode("utf-8", "replace"))
                    elif value is not None:
                        found.append(str(value))
        return found
    finally:
        conn.close()


def raw_bytes(db_path: Path) -> bytes:
    """Main database file plus any WAL/journal sidecars, after a checkpoint."""

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()
    blob = db_path.read_bytes()
    for suffix in ("-wal", "-journal"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file():
            blob += sidecar.read_bytes()
    return blob


class TestRealAuthorityExport:
    def test_export_returns_only_the_requested_scope(self, tmp_path: Path) -> None:
        settings, service, admin_service, grant = make_stack(
            tmp_path, memory_scopes=(SCOPE, OTHER_SCOPE)
        )
        ctx = grant.caller_context()
        mine = add(service, ctx, content=CANARY, key="key-mine-01")
        add(service, ctx, content="a different scope entirely", key="key-other-01", scope=OTHER_SCOPE)

        result = admin_service.export(ctx, scope=SCOPE)
        assert [m.memory_id for m in result.memories] == [mine["memory_id"]]
        assert result.memories[0].content == CANARY

        other = admin_service.export(ctx, scope=OTHER_SCOPE)
        assert [m.content for m in other.memories] == ["a different scope entirely"]

    def test_export_refuses_a_scope_the_grant_cannot_reach(self, tmp_path: Path) -> None:
        _settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content=CANARY, key="key-0001")
        with pytest.raises(admin.AdminNotAuthorizedError):
            admin_service.export(ctx, scope=OTHER_SCOPE)

    def test_a_read_only_grant_cannot_bulk_export(self, tmp_path: Path) -> None:
        settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content=CANARY, key="key-0001")

        # Downgrade the *stored* grant: the SQL predicate, not the context
        # object, has to be what refuses the export.
        conn = sqlite3.connect(settings.db_path)
        conn.execute(
            "UPDATE client_grants SET oauth_scopes_json = ? WHERE grant_id = ?",
            (json.dumps(["memory:read", "memory:write"]), grant.grant_id),
        )
        conn.commit()
        conn.close()

        rows = service.repository.export_scoped(grant_id=grant.grant_id, scope=SCOPE)
        assert rows == ()

    def test_a_revoked_grant_cannot_export(self, tmp_path: Path) -> None:
        settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content=CANARY, key="key-0001")
        conn = sqlite3.connect(settings.db_path)
        conn.execute(
            "UPDATE client_grants SET revoked_at = ? WHERE grant_id = ?",
            ("2026-08-05T00:00:00+00:00", grant.grant_id),
        )
        conn.commit()
        conn.close()
        assert service.repository.export_scoped(grant_id=grant.grant_id, scope=SCOPE) == ()

    def test_export_excludes_tombstones_unless_asked(self, tmp_path: Path) -> None:
        _settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        row = add(service, ctx, content=CANARY, key="key-0001")
        remove(service, ctx, memory_id=row["memory_id"], revision=row["revision"], key="key-0002")

        assert admin_service.export(ctx, scope=SCOPE).memories == ()
        with_dead = admin_service.export(ctx, scope=SCOPE, include_tombstones=True)
        assert len(with_dead.memories) == 1
        assert with_dead.memories[0].deleted_at is not None
        assert with_dead.document()["tombstone_count"] == 1


class TestSecondLocalClientGrant:
    """Regression: two local clients for one owner must both bind (6.10 smoke).

    ``grant_id`` used to be ``grant-local-<owner>-<generation>`` with no client
    component, so a second local client collided with the operator grant on
    ``UNIQUE(grant_id, owner_id)`` and could not be created at all.
    """

    def test_a_second_client_gets_its_own_grant(self, tmp_path: Path) -> None:
        settings, service, _admin, local = make_stack(tmp_path)
        seeder = authority.ensure_local_grant(
            settings.db_path, memory_scopes=(OTHER_SCOPE,), client_id="seed-writer"
        )
        assert seeder.grant_id != local.grant_id
        assert seeder.owner_id == local.owner_id
        assert seeder.memory_scopes == (OTHER_SCOPE,)

        conn = sqlite3.connect(settings.db_path)
        try:
            rows = conn.execute(
                "SELECT client_id, grant_id FROM client_grants WHERE revoked_at IS NULL "
                "AND unlinked_at IS NULL ORDER BY client_id"
            ).fetchall()
        finally:
            conn.close()
        clients = {row[0]: row[1] for row in rows}
        assert {"local-operator", "seed-writer"} <= set(clients)
        assert len({row[1] for row in rows}) == len(rows), "grant ids must be unique"

    def test_the_default_client_keeps_its_historical_grant_id(self, tmp_path: Path) -> None:
        settings, _service, _admin, local = make_stack(tmp_path)
        assert local.grant_id == f"grant-local-{local.owner_id}-1"

    def test_each_client_only_reaches_its_own_scope(self, tmp_path: Path) -> None:
        settings, service, admin_service, local = make_stack(tmp_path)
        seeder = authority.ensure_local_grant(
            settings.db_path, memory_scopes=(OTHER_SCOPE,), client_id="seed-writer"
        )
        add(service, seeder.caller_context(), content=CANARY, key="key-0007", scope=OTHER_SCOPE)
        # the operator grant cannot reach the seeder's scope at all
        with pytest.raises(admin.AdminNotAuthorizedError):
            admin_service.export(local.caller_context(), scope=OTHER_SCOPE)
        assert service.repository.export_scoped(grant_id=local.grant_id, scope=OTHER_SCOPE) == ()
        assert len(service.repository.export_scoped(grant_id=seeder.grant_id, scope=OTHER_SCOPE)) == 1


class TestRealAuthorityRestore:
    def test_restore_brings_a_tombstone_back_with_a_new_revision(self, tmp_path: Path) -> None:
        _settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        row = add(service, ctx, content=CANARY, key="key-0001")
        removed = remove(service, ctx, memory_id=row["memory_id"], revision=row["revision"], key="key-0002")

        restored = admin_service.restore(
            ctx,
            memory_id=row["memory_id"],
            scope=SCOPE,
            expected_revision=removed["revision"],
            idempotency_key="key-0003",
        )
        assert restored.revision == removed["revision"] + 1

        found = data_of(service.get(ctx, models.GetRequest(memory_id=row["memory_id"], scope=SCOPE)))
        assert found["memory"]["content"] == CANARY
        assert found["memory"]["revision"] == restored.revision

    def test_restore_with_a_stale_revision_is_a_conflict(self, tmp_path: Path) -> None:
        _settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        row = add(service, ctx, content=CANARY, key="key-0001")
        remove(service, ctx, memory_id=row["memory_id"], revision=row["revision"], key="key-0002")
        with pytest.raises(admin.AdminRevisionConflictError):
            admin_service.restore(
                ctx,
                memory_id=row["memory_id"],
                scope=SCOPE,
                expected_revision=1,
                idempotency_key="key-0003",
            )


class TestRealAuthorityPurge:
    def _purged(self, tmp_path: Path) -> tuple[Any, ...]:
        settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        row = add(service, ctx, content=CANARY, key="key-add-0001")
        removed = remove(
            service, ctx, memory_id=row["memory_id"], revision=row["revision"], key="key-rm-0001"
        )
        result = admin_service.purge(
            ctx,
            memory_id=row["memory_id"],
            scope=SCOPE,
            expected_revision=removed["revision"],
            idempotency_key="key-purge-0001",
        )
        return settings, service, admin_service, grant, ctx, row, removed, result

    def test_purge_writes_exactly_one_final_revision_event(self, tmp_path: Path) -> None:
        settings, _service, _admin, _grant, _ctx, row, removed, result = self._purged(tmp_path)
        assert result.final_revision == removed["revision"] + 1
        conn = sqlite3.connect(settings.db_path)
        try:
            events = conn.execute(
                "SELECT revision, operation FROM memory_events WHERE memory_id = ? ORDER BY revision",
                (row["memory_id"],),
            ).fetchall()
        finally:
            conn.close()
        assert events[-1] == (result.final_revision, "purge")
        assert [e[0] for e in events] == sorted({e[0] for e in events}), "revisions must be unique"

    def test_purge_removes_the_row_and_every_index(self, tmp_path: Path) -> None:
        settings, _service, _admin, _grant, _ctx, row, _removed, _result = self._purged(tmp_path)
        conn = sqlite3.connect(settings.db_path)
        try:
            for table, column in (
                ("memories", "id"),
                ("memory_metadata", "memory_id"),
                ("keywords", "memory_id"),
                ("memory_embeddings", "memory_id"),
            ):
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (row["memory_id"],)
                ).fetchone()[0]
                assert count == 0, f"{table} still holds the purged memory"
            fts = conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?", ("Ratatoskr",)
            ).fetchone()[0]
            assert fts == 0
        finally:
            conn.close()

    def test_purged_content_is_absent_from_every_table(self, tmp_path: Path) -> None:
        settings, *_ = self._purged(tmp_path)
        values = all_text_values(settings.db_path)
        assert values, "the scan found no rows at all - it is not proving anything"
        offenders = [value for value in values if CANARY in value]
        assert offenders == [], f"purged content survives in the database: {offenders[:2]}"

    def test_purged_content_is_absent_from_the_raw_file(self, tmp_path: Path) -> None:
        settings, *_ = self._purged(tmp_path)
        blob = raw_bytes(settings.db_path)
        assert CANARY.encode("utf-8") not in blob, (
            "purged content is still readable in the raw database file; "
            "secure_delete did not zero the freed pages"
        )

    def test_old_idempotency_key_fails_closed_after_purge(self, tmp_path: Path) -> None:
        _settings, service, _admin, _grant, ctx, _row, _removed, _result = self._purged(tmp_path)
        replayed = service.add(
            ctx,
            models.AddRequest(
                content=CANARY,
                scope=SCOPE,
                kind="note",
                tags=("canary",),
                idempotency_key="key-add-0001",
            ),
        )
        assert error_of(replayed)["code"] == "idempotency_key_purged"

    def test_purged_memory_cannot_be_restored(self, tmp_path: Path) -> None:
        _settings, _service, admin_service, _grant, ctx, row, _removed, result = self._purged(
            tmp_path
        )
        with pytest.raises(admin.AdminNotAuthorizedError):
            admin_service.restore(
                ctx,
                memory_id=row["memory_id"],
                scope=SCOPE,
                expected_revision=result.final_revision,
                idempotency_key="key-restore-after-purge",
            )

    def test_purged_memory_is_invisible_to_reads(self, tmp_path: Path) -> None:
        _settings, service, _admin, _grant, ctx, row, _removed, _result = self._purged(tmp_path)
        found = service.get(ctx, models.GetRequest(memory_id=row["memory_id"], scope=SCOPE))
        assert error_of(found)["code"] == "not_found"
        results = data_of(service.search(ctx, models.SearchRequest(query="Ratatoskr", scope=SCOPE)))
        assert results["memories"] == []

    def test_idempotency_deny_rows_keep_no_content(self, tmp_path: Path) -> None:
        settings, *_ = self._purged(tmp_path)
        conn = sqlite3.connect(settings.db_path)
        try:
            rows = conn.execute(
                "SELECT operation, result_json, payload_digest, purged_at FROM mcp_idempotency"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "the deny tombstones were deleted instead of scrubbed"
        for operation, result_json, payload_digest, purged_at in rows:
            assert result_json is None, f"{operation} still carries a replayable result"
            assert purged_at is not None
            assert CANARY not in str(payload_digest)

    def test_external_read_back_after_purge(self, tmp_path: Path) -> None:
        """A fresh connection, opened after the fact, sees a consistent DB."""

        settings, *_ = self._purged(tmp_path)
        conn = sqlite3.connect(f"{Path(settings.db_path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE operation = 'purge'"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_stale_expected_revision_deletes_nothing(self, tmp_path: Path) -> None:
        settings, service, admin_service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        row = add(service, ctx, content=CANARY, key="key-0001")
        remove(service, ctx, memory_id=row["memory_id"], revision=row["revision"], key="key-0002")
        with pytest.raises(admin.AdminRevisionConflictError):
            admin_service.purge(
                ctx,
                memory_id=row["memory_id"],
                scope=SCOPE,
                expected_revision=1,
                idempotency_key="key-0003",
            )
        conn = sqlite3.connect(settings.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        finally:
            conn.close()

    def test_purge_audit_is_written_next_to_the_database(self, tmp_path: Path) -> None:
        settings, *_ = self._purged(tmp_path)
        path = admin.admin_audit_path_for(settings.db_path)
        assert path.is_file()
        raw = path.read_text(encoding="utf-8")
        assert CANARY not in raw
        assert str(settings.db_path) not in raw
        entries = admin.AdminAuditLog(path).entries()
        assert entries[-1].operation == "memory_purge"
        assert entries[-1].outcome == "ok"
