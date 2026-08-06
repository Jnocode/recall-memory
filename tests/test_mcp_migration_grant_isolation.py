"""Finding M-1 remediation — MIGRATION grants are quarantine-only.

Phase 10 task 10.6 security review, finding M-1: the shared read predicate in
``mcp_repository.py`` treats ``grant_kind='MIGRATION'`` as an unconditional
authorization bypass (``authorized_grant.grant_kind = 'MIGRATION' OR EXISTS
(... memory:read ...)``), so a MIGRATION grant reads memories even though its
``oauth_scopes_json`` is an empty list.

That bypass is *load-bearing* for the legacy quarantine workflow (task 1.5:
migrated rows are readable only through the migration grant at
``legacy:unscoped``), so it cannot simply be deleted.  The fix is a
``client_facing`` switch that adds ``grant_kind = 'OAUTH'`` to the SQL join, so
the MCP tool surface -- which is what an OAuth client actually reaches --
cannot be served by a MIGRATION grant even if one were ever resolved for it.

Everything below asserts on a real migrated SQLite file, not a mock.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recall.mcp_repository import RecallMCPRepository  # noqa: E402
from recall.migrations import BootstrapIdentity, migrate_database  # noqa: E402

from test_mcp_schema_migration import (  # noqa: E402
    LEGACY_ROWS,
    _create_legacy_db,
)

LEGACY_SCOPE = "legacy:unscoped"


@pytest.fixture
def bootstrap() -> BootstrapIdentity:
    return BootstrapIdentity(
        owner_id="owner-m1-test",
        migration_grant_id="grant-m1-migration",
        created_at="2026-08-06T00:00:00+00:00",
    )


@pytest.fixture
def migrated(tmp_path: Path, bootstrap: BootstrapIdentity) -> tuple[Path, BootstrapIdentity]:
    db_path = tmp_path / "m1.db"
    _create_legacy_db(db_path)
    migrate_database(
        db_path, backup_path=tmp_path / "m1-backup.db", bootstrap=bootstrap
    )
    return db_path, bootstrap


def _migration_grant_has_no_oauth_scopes(db_path: Path, grant_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT grant_kind, oauth_scopes_json FROM client_grants WHERE grant_id = ?",
            (grant_id,),
        ).fetchone()
    assert row is not None, "migration grant missing"
    assert row[0] == "MIGRATION"
    assert row[1] == "[]", f"expected zero OAuth scopes, got {row[1]!r}"


class TestQuarantineWorkflowStillWorks:
    """The migration grant must keep its legacy-scope read capability."""

    def test_default_read_path_still_serves_the_quarantine(
        self, migrated: tuple[Path, BootstrapIdentity]
    ) -> None:
        db_path, boot = migrated
        _migration_grant_has_no_oauth_scopes(db_path, boot.migration_grant_id)
        repository = RecallMCPRepository(db_path)

        found = repository.search_scoped(
            grant_id=boot.migration_grant_id, scope=LEGACY_SCOPE, query="docker"
        )
        assert [r.memory_id for r in found] == ["legacy-alpha"]
        assert found[0].content == LEGACY_ROWS[0][1]

        got = repository.get_scoped_memory(
            grant_id=boot.migration_grant_id,
            scope=LEGACY_SCOPE,
            memory_id="legacy-alpha",
        )
        assert got is not None and got.memory_id == "legacy-alpha"

        recent = repository.recent_scoped_memories(
            grant_id=boot.migration_grant_id, scope=LEGACY_SCOPE, limit=10
        )
        assert {r.memory_id for r in recent} == {"legacy-alpha", "legacy-beta"}


class TestClientFacingReadsRejectMigrationGrants:
    """``client_facing=True`` must fail closed for a MIGRATION grant."""

    def test_search_is_empty(self, migrated: tuple[Path, BootstrapIdentity]) -> None:
        db_path, boot = migrated
        repository = RecallMCPRepository(db_path)
        assert (
            repository.search_scoped(
                grant_id=boot.migration_grant_id,
                scope=LEGACY_SCOPE,
                query="docker",
                client_facing=True,
            )
            == ()
        )

    def test_get_is_none(self, migrated: tuple[Path, BootstrapIdentity]) -> None:
        db_path, boot = migrated
        repository = RecallMCPRepository(db_path)
        assert (
            repository.get_scoped_memory(
                grant_id=boot.migration_grant_id,
                scope=LEGACY_SCOPE,
                memory_id="legacy-alpha",
                client_facing=True,
            )
            is None
        )

    def test_recent_is_empty(self, migrated: tuple[Path, BootstrapIdentity]) -> None:
        db_path, boot = migrated
        repository = RecallMCPRepository(db_path)
        assert (
            repository.recent_scoped_memories(
                grant_id=boot.migration_grant_id,
                scope=LEGACY_SCOPE,
                limit=10,
                client_facing=True,
            )
            == ()
        )

    def test_rejection_is_not_a_scope_typo(
        self, migrated: tuple[Path, BootstrapIdentity]
    ) -> None:
        """Prove the empty result is caused by grant_kind, not by the scope.

        Same grant, same scope, same query: only ``client_facing`` differs.
        """
        db_path, boot = migrated
        repository = RecallMCPRepository(db_path)
        permissive = repository.search_scoped(
            grant_id=boot.migration_grant_id, scope=LEGACY_SCOPE, query="docker"
        )
        strict = repository.search_scoped(
            grant_id=boot.migration_grant_id,
            scope=LEGACY_SCOPE,
            query="docker",
            client_facing=True,
        )
        assert permissive != ()
        assert strict == ()


class TestClientFacingReadsStillServeOauthGrants:
    """No over-blocking: a normal OAuth read grant is unaffected."""

    def _make_oauth_grant(
        self, db_path: Path, boot: BootstrapIdentity, grant_id: str
    ) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                INSERT INTO client_grants (
                    grant_id, owner_id, issuer, subject, client_id,
                    grant_generation, grant_kind, source_client,
                    oauth_scopes_json, memory_scope_patterns_json,
                    binding_challenge_id, created_at
                ) VALUES (?, ?, 'https://issuer.test', 'subject-1', 'client-1',
                          1, 'OAUTH', 'kiro', '["memory:read"]',
                          ?, NULL, '2026-08-06T00:00:00+00:00')
                """,
                (grant_id, boot.owner_id, f'["{LEGACY_SCOPE}"]'),
            )

    def test_oauth_grant_reads_identically_with_and_without_the_switch(
        self, migrated: tuple[Path, BootstrapIdentity]
    ) -> None:
        db_path, boot = migrated
        self._make_oauth_grant(db_path, boot, "grant-m1-oauth")
        repository = RecallMCPRepository(db_path)

        permissive = repository.search_scoped(
            grant_id="grant-m1-oauth", scope=LEGACY_SCOPE, query="docker"
        )
        strict = repository.search_scoped(
            grant_id="grant-m1-oauth",
            scope=LEGACY_SCOPE,
            query="docker",
            client_facing=True,
        )
        assert [r.memory_id for r in permissive] == ["legacy-alpha"]
        assert permissive == strict

        assert (
            repository.get_scoped_memory(
                grant_id="grant-m1-oauth",
                scope=LEGACY_SCOPE,
                memory_id="legacy-alpha",
                client_facing=True,
            )
            is not None
        )
        assert (
            repository.recent_scoped_memories(
                grant_id="grant-m1-oauth",
                scope=LEGACY_SCOPE,
                limit=10,
                client_facing=True,
            )
            != ()
        )
