"""Finding M-1 remediation at the client-facing boundary.

Phase 10 task 10.6 security review, finding M-1: ``grant_kind='MIGRATION'``
short-circuits the OAuth-scope check in the shared read predicate.  That
bypass has to survive for the legacy quarantine workflow (task 1.5), so the
core keeps it behind an explicit opt-in and *this* layer -- the only one an
OAuth client can reach -- pins ``grant_kind = 'OAUTH'``.

Everything below runs against a real migrated authority database created by
``provisioning.initialize`` inside ``tmp_path``.  A freshly initialised
authority contains exactly one grant and it is the MIGRATION grant, which is
what makes this test possible without hand-forging a grant row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from recall_memory_mcp import authority as authority_mod
from recall_memory_mcp import provisioning, repository as repo
from recall_memory_mcp.settings import ServerSettings

pytest.importorskip("recall.mcp_repository", reason="the canonical recall-sqlite core is required")

from recall.mcp_repository import RecallMCPRepository  # noqa: E402

from test_admin import STUB_IDENTITY  # noqa: E402
from test_breakglass import seed_memory  # noqa: E402

LEGACY_SCOPE = "legacy:unscoped"
MEMORY_ID = "mem-m1-canary"


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
def quarantined(tmp_path: Path, isolated_env: dict[str, str]) -> tuple[Path, str]:
    """Real authority DB + one legacy row owned by the MIGRATION grant."""

    env = dict(isolated_env)
    env["RECALL_MCP_CONFIG_DIR"] = str(tmp_path / "authority")
    env["RECALL_MCP_DB_PATH"] = str(tmp_path / "authority" / "recall.db")
    settings = ServerSettings.from_env(env)
    provisioning.initialize(settings)
    db_path = Path(settings.db_path)
    seed_memory(db_path, MEMORY_ID)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT grant_id, grant_kind, oauth_scopes_json FROM client_grants"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected exactly one bootstrap grant, got {rows!r}"
    grant_id, grant_kind, oauth_scopes = rows[0]
    assert grant_kind == "MIGRATION"
    assert oauth_scopes == "[]"
    return db_path, str(grant_id)


def _client_repo(db_path: Path) -> authority_mod.SqliteAuthorityRepository:
    return authority_mod.SqliteAuthorityRepository(db_path, identity=STUB_IDENTITY)


class TestMigrationGrantCannotReachTheToolSurface:
    def test_core_quarantine_read_still_works(
        self, quarantined: tuple[Path, str]
    ) -> None:
        """Control: the migration/review path is intentionally unaffected."""
        db_path, grant_id = quarantined
        core = RecallMCPRepository(db_path)
        row = core.get_scoped_memory(
            grant_id=grant_id, scope=LEGACY_SCOPE, memory_id=MEMORY_ID
        )
        assert row is not None and row.memory_id == MEMORY_ID
        assert core.recent_scoped_memories(
            grant_id=grant_id, scope=LEGACY_SCOPE, limit=10
        ) != ()

    def test_client_facing_get_is_none(self, quarantined: tuple[Path, str]) -> None:
        db_path, grant_id = quarantined
        assert (
            _client_repo(db_path).get_scoped_memory(
                grant_id=grant_id, scope=LEGACY_SCOPE, memory_id=MEMORY_ID
            )
            is None
        )

    def test_client_facing_recent_is_empty(
        self, quarantined: tuple[Path, str]
    ) -> None:
        db_path, grant_id = quarantined
        assert (
            _client_repo(db_path).recent_scoped_memories(
                grant_id=grant_id, scope=LEGACY_SCOPE, limit=10
            )
            == ()
        )

    def test_client_facing_search_is_empty(
        self, quarantined: tuple[Path, str]
    ) -> None:
        db_path, grant_id = quarantined
        assert (
            _client_repo(db_path).search_scoped(
                grant_id=grant_id, scope=LEGACY_SCOPE, query="canary", limit=10
            )
            == ()
        )

    def test_client_facing_cardinality_is_zero(
        self, quarantined: tuple[Path, str]
    ) -> None:
        """``memory_status`` must not leak the quarantine's existence either."""
        db_path, grant_id = quarantined
        assert (
            _client_repo(db_path).scope_cardinality(
                grant_id=grant_id, scope=LEGACY_SCOPE
            )
            == 0
        )

    def test_read_predicate_pins_oauth_and_keeps_no_bypass(self) -> None:
        predicate = authority_mod._READ_GRANT_PREDICATE
        assert "authorized_grant.grant_kind = 'OAUTH'" in predicate
        assert "MIGRATION" not in predicate

    def test_delegated_reads_opt_into_the_strict_core_predicate(self) -> None:
        """Every delegated read must forward ``client_facing=True``."""

        seen: list[tuple[str, dict[str, object]]] = []

        class Spy:
            def __getattr__(self, name: str):
                def _call(**kwargs: object) -> tuple[()]:
                    seen.append((name, kwargs))
                    return ()

                return _call

        # Constructed without __init__ so no database is required: this test
        # is only about the keyword the adapter forwards to the core.
        adapter = authority_mod.SqliteAuthorityRepository.__new__(
            authority_mod.SqliteAuthorityRepository
        )
        adapter._core = repo  # only used for error translation, never hit here
        adapter._delegate = Spy()

        adapter.search_scoped(grant_id="g", scope="s", query="q", limit=1)
        adapter.get_scoped_memory(grant_id="g", scope="s", memory_id="m")
        adapter.recent_scoped_memories(grant_id="g", scope="s", limit=1)

        assert [name for name, _ in seen] == [
            "search_scoped",
            "get_scoped_memory",
            "recent_scoped_memories",
        ]
        for name, kwargs in seen:
            assert kwargs.get("client_facing") is True, name
