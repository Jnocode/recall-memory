"""Authority wiring tests (prerequisite for task 6.3 ``serve``).

Every test builds a *real* SQLite authority database in ``tmp_path`` through
``provisioning.initialize`` and drives the real service on top of it.  There
is no mock repository here: the point of this module is to prove that the
storage-free service and the canonical ``recall-sqlite`` core actually fit
together, including the error taxonomy and the embedding-profile lifecycle.

Nothing in this file may read the operator's private database: settings are
always built from an explicit, synthetic environment mapping rooted at
``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import pytest

from recall_memory_mcp import authority, models, provisioning
from recall_memory_mcp import repository as repo
from recall_memory_mcp.settings import ServerSettings

pytest.importorskip("recall.mcp_repository", reason="the canonical recall-sqlite core is required")

SCOPE = "global"
OTHER_SCOPE = "project:other"
DIMENSION = 8

STUB_IDENTITY = authority.EmbeddingIdentity(
    provider="test-provider",
    model="stub-embed-8",
    endpoint_identity_hash="0" * 64,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class StubEmbedder:
    """Deterministic offline vector provider with a declared identity."""

    def __init__(
        self,
        *,
        available: bool = True,
        dimension: int = DIMENSION,
        identity: authority.EmbeddingIdentity = STUB_IDENTITY,
    ) -> None:
        self.available = available
        self.dimension = dimension
        self.identity = identity

    def encode(self, text: str) -> dict[int, bytes]:
        if not self.available:
            raise repo.EmbeddingUnavailableError("provider down")
        seed = abs(hash(text)) % 997 or 1
        vector = [((seed * (index + 1)) % 101) / 100.0 for index in range(self.dimension)]
        return {authority.DEFAULT_GENERATION: authority.pack_vector(vector)}


def make_settings(tmp_path: Path, **overrides: str) -> ServerSettings:
    env = {
        "RECALL_MCP_CONFIG_DIR": str(tmp_path / "authority"),
        "RECALL_MCP_MODE": "local",
        "RECALL_MCP_HOST": "127.0.0.1",
        "RECALL_MCP_PORT": "19881",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"),
        "TEMP": str(tmp_path / "temp"),
    }
    env.update(overrides)
    return ServerSettings.from_env(env)


def initialised(tmp_path: Path, **overrides: str) -> ServerSettings:
    settings = make_settings(tmp_path, **overrides)
    provisioning.initialize(settings)
    return settings


def make_stack(
    tmp_path: Path,
    *,
    embedder: StubEmbedder | None = None,
    memory_scopes: tuple[str, ...] = (SCOPE,),
) -> tuple[ServerSettings, Any, authority.AuthorityGrant]:
    settings = initialised(tmp_path)
    service = authority.build_service(
        settings,
        embedder=embedder or StubEmbedder(),
        env={},
        digest_key=b"deterministic-test-digest-key",
        clock=lambda: "2026-08-04T00:00:00+00:00",
    )
    grant = authority.ensure_local_grant(settings.db_path, memory_scopes=memory_scopes)
    return settings, service, grant


def add(service: Any, ctx: Any, *, content: str, key: str, scope: str = SCOPE) -> Any:
    return service.add(
        ctx,
        models.AddRequest(
            content=content, scope=scope, kind="note", tags=("canary",), idempotency_key=key
        ),
    )


def data_of(envelope: Any) -> dict[str, Any]:
    payload = envelope.model_dump(mode="json")
    assert "error" not in payload, f"unexpected error envelope: {payload}"
    return payload["data"]


def error_of(envelope: Any) -> dict[str, Any]:
    payload = envelope.model_dump(mode="json")
    assert "error" in payload, f"expected an error envelope, got: {payload}"
    return payload["error"]


def rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# embedder
# ---------------------------------------------------------------------------


class TestEmbedder:
    def test_pack_vector_is_little_endian_float32(self) -> None:
        blob = authority.pack_vector([1.0, -2.5, 0.0])
        assert len(blob) == 12
        assert struct.unpack("<3f", blob) == (1.0, -2.5, 0.0)

    def test_empty_vector_is_refused(self) -> None:
        with pytest.raises(repo.EmbeddingUnavailableError):
            authority.pack_vector([])

    def test_unreachable_provider_raises_typed_error(self) -> None:
        embedder = authority.RecallEmbedder(
            embed_fn=lambda _text: None, model="m", endpoint="http://127.0.0.1:1/v1/embeddings"
        )
        with pytest.raises(repo.EmbeddingUnavailableError):
            embedder.encode("anything")

    def test_provider_exception_never_escapes_untyped(self) -> None:
        def boom(_text: str) -> list[float]:
            raise RuntimeError("connection reset by C:\\secret\\path")

        embedder = authority.RecallEmbedder(
            embed_fn=boom, model="m", endpoint="http://127.0.0.1:1/v1/embeddings"
        )
        with pytest.raises(repo.EmbeddingUnavailableError) as caught:
            embedder.encode("anything")
        assert "secret" not in str(caught.value)

    def test_endpoint_is_hashed_not_stored(self) -> None:
        embedder = authority.RecallEmbedder(
            embed_fn=lambda _t: [1.0], model="m", endpoint="http://user:pw@host:1234/v1/embeddings"
        )
        assert "pw" not in embedder.identity.endpoint_identity_hash
        assert "host" not in embedder.identity.endpoint_identity_hash
        assert len(embedder.identity.endpoint_identity_hash) == 64

    def test_encode_returns_one_generation_keyed_blob(self) -> None:
        embedder = authority.RecallEmbedder(
            embed_fn=lambda _t: [0.5, 0.25], model="m", endpoint="http://127.0.0.1:1"
        )
        blobs = embedder.encode("hello")
        assert set(blobs) == {authority.DEFAULT_GENERATION}
        assert struct.unpack("<2f", blobs[authority.DEFAULT_GENERATION]) == (0.5, 0.25)


# ---------------------------------------------------------------------------
# health / cardinality
# ---------------------------------------------------------------------------


class TestHealth:
    def test_reports_schema_version_of_a_real_database(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        repository = authority.SqliteAuthorityRepository(
            settings.db_path, identity=STUB_IDENTITY
        )
        health = repository.health()
        assert health.reachable is True
        assert health.schema_version >= 1
        assert health.embedding_generation is None

    def test_missing_database_is_unreachable_not_an_exception(self, tmp_path: Path) -> None:
        repository = authority.SqliteAuthorityRepository(
            tmp_path / "nope.db", identity=STUB_IDENTITY
        )
        health = repository.health()
        assert health.reachable is False
        assert health.schema_version == 0

    def test_health_reports_the_active_generation_after_a_write(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        add(service, grant.caller_context(), content="health probe", key="idem-health-1")
        health = service.repository.health()
        assert health.embedding_generation == authority.DEFAULT_GENERATION

    def test_scope_cardinality_only_counts_the_authorized_scope(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path, memory_scopes=(SCOPE, OTHER_SCOPE))
        ctx = grant.caller_context()
        add(service, ctx, content="one", key="idem-card-1")
        add(service, ctx, content="two", key="idem-card-2")
        add(service, ctx, content="three", key="idem-card-3", scope=OTHER_SCOPE)

        repository = service.repository
        assert repository.scope_cardinality(grant_id=grant.grant_id, scope=SCOPE) == 2
        assert repository.scope_cardinality(grant_id=grant.grant_id, scope=OTHER_SCOPE) == 1

    def test_scope_cardinality_of_a_foreign_grant_is_zero(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        add(service, grant.caller_context(), content="mine", key="idem-card-4")
        assert (
            service.repository.scope_cardinality(grant_id="grant-does-not-exist", scope=SCOPE) == 0
        )

    def test_blank_arguments_are_validation_errors(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        repository = authority.SqliteAuthorityRepository(
            settings.db_path, identity=STUB_IDENTITY
        )
        with pytest.raises(repo.RepositoryValidationError):
            repository.scope_cardinality(grant_id="", scope=SCOPE)


# ---------------------------------------------------------------------------
# local grant
# ---------------------------------------------------------------------------


class TestLocalGrant:
    def test_requires_an_initialised_database(self, tmp_path: Path) -> None:
        with pytest.raises(authority.AuthorityNotInitialisedError):
            authority.ensure_local_grant(tmp_path / "missing.db")

    def test_is_idempotent_and_audited(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        first = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        second = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        assert first == second

        grants = rows(
            settings.db_path,
            "SELECT grant_id, grant_kind FROM client_grants WHERE issuer = ?",
            (authority.LOCAL_ISSUER,),
        )
        assert grants == [(first.grant_id, "OAUTH")]
        events = rows(
            settings.db_path,
            "SELECT operation FROM owner_link_events WHERE grant_id = ?",
            (first.grant_id,),
        )
        assert events == [("initial_bind",)]

    def test_widening_scopes_creates_a_new_generation_and_retires_the_old(
        self, tmp_path: Path
    ) -> None:
        settings = initialised(tmp_path)
        first = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        second = authority.ensure_local_grant(settings.db_path, memory_scopes=(OTHER_SCOPE,))
        assert second.grant_id != first.grant_id
        assert set(second.memory_scopes) == {SCOPE, OTHER_SCOPE}

        retired = rows(
            settings.db_path,
            "SELECT unlinked_at IS NOT NULL FROM client_grants WHERE grant_id = ?",
            (first.grant_id,),
        )
        assert retired == [(1,)]
        operations = {
            row[0]
            for row in rows(settings.db_path, "SELECT operation FROM owner_link_events", ())
        }
        assert operations == {"initial_bind", "scope_change"}

    def test_audit_event_stores_a_digest_not_the_details(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        grant = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        (digest,), = rows(
            settings.db_path,
            "SELECT details_digest FROM owner_link_events WHERE grant_id = ?",
            (grant.grant_id,),
        )
        assert len(digest) == 64
        assert SCOPE not in digest


# ---------------------------------------------------------------------------
# embedding profiles
# ---------------------------------------------------------------------------


class TestEmbeddingProfiles:
    def test_first_write_provisions_exactly_one_active_generation(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        add(service, grant.caller_context(), content="profile probe", key="idem-prof-1")

        profiles = rows(
            settings.db_path,
            "SELECT scope, generation, provider, model, dimension, status "
            "FROM embedding_profiles",
        )
        assert profiles == [
            (SCOPE, 1, STUB_IDENTITY.provider, STUB_IDENTITY.model, DIMENSION, "ACTIVE")
        ]

    def test_second_write_reuses_the_same_generation(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content="first", key="idem-prof-2")
        add(service, ctx, content="second", key="idem-prof-3")
        assert rows(settings.db_path, "SELECT COUNT(*) FROM embedding_profiles") == [(1,)]

    def test_stored_vector_matches_the_embedder_output(self, tmp_path: Path) -> None:
        embedder = StubEmbedder()
        settings, service, grant = make_stack(tmp_path, embedder=embedder)
        result = data_of(
            add(service, grant.caller_context(), content="vector probe", key="idem-prof-4")
        )
        expected = embedder.encode("vector probe")[authority.DEFAULT_GENERATION]
        stored = rows(
            settings.db_path,
            "SELECT embedding_blob, dimension FROM memory_embeddings WHERE memory_id = ?",
            (result["memory_id"],),
        )
        assert stored == [(expected, DIMENSION)]

    def test_a_different_embedder_identity_fails_closed(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content="original embedder", key="idem-prof-5")

        other = StubEmbedder(
            identity=authority.EmbeddingIdentity(
                provider="other", model="other-model", endpoint_identity_hash="1" * 64
            )
        )
        swapped = authority.build_service(
            settings,
            embedder=other,
            env={},
            digest_key=b"deterministic-test-digest-key",
            clock=lambda: "2026-08-04T00:00:01+00:00",
        )
        error = error_of(add(swapped, ctx, content="mismatched", key="idem-prof-6"))
        assert error["code"] == models.ErrorCode.EMBEDDING_UNAVAILABLE.value
        assert rows(settings.db_path, "SELECT COUNT(*) FROM embedding_profiles") == [(1,)]

    def test_a_different_dimension_fails_closed(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content="eight dimensions", key="idem-prof-7")

        wider = authority.build_service(
            settings,
            embedder=StubEmbedder(dimension=16),
            env={},
            digest_key=b"deterministic-test-digest-key",
            clock=lambda: "2026-08-04T00:00:02+00:00",
        )
        error = error_of(add(wider, ctx, content="sixteen dimensions", key="idem-prof-8"))
        assert error["code"] == models.ErrorCode.EMBEDDING_UNAVAILABLE.value


# ---------------------------------------------------------------------------
# end-to-end service behaviour on a real database
# ---------------------------------------------------------------------------


class TestServiceOnRealAuthority:
    def test_add_then_get_reads_the_content_back(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        added = data_of(add(service, ctx, content="canary alpha", key="idem-rt-1"))
        assert added["revision"] == 1

        fetched = data_of(
            service.get(ctx, models.GetRequest(memory_id=added["memory_id"], scope=SCOPE))
        )
        assert fetched["memory"]["content"] == "canary alpha"
        assert fetched["memory"]["revision"] == 1
        assert fetched["memory"]["data_trust"] == models.DATA_TRUST_UNTRUSTED

    def test_search_finds_the_stored_memory(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content="the sky is documented as blue", key="idem-rt-2")
        found = data_of(service.search(ctx, models.SearchRequest(query="documented", scope=SCOPE)))
        assert [memory["content"] for memory in found["memories"]] == [
            "the sky is documented as blue"
        ]

    def test_recent_returns_the_newest_first(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        clock = iter(
            [
                "2026-08-04T00:00:01+00:00",
                "2026-08-04T00:00:02+00:00",
                "2026-08-04T00:00:03+00:00",
                "2026-08-04T00:00:04+00:00",
            ]
        )
        service = authority.build_service(
            settings,
            embedder=StubEmbedder(),
            env={},
            digest_key=b"deterministic-test-digest-key",
            clock=lambda: next(clock),
        )
        grant = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        ctx = grant.caller_context()
        add(service, ctx, content="older", key="idem-rt-3")
        add(service, ctx, content="newer", key="idem-rt-4")
        recent = data_of(service.recent(ctx, models.RecentRequest(scope=SCOPE, limit=5)))
        assert [memory["content"] for memory in recent["memories"]] == ["newer", "older"]

    def test_replace_bumps_the_revision(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        added = data_of(add(service, ctx, content="first draft", key="idem-rt-5"))
        replaced = data_of(
            service.replace(
                ctx,
                models.ReplaceRequest(
                    memory_id=added["memory_id"],
                    expected_revision=1,
                    content="second draft",
                    scope=SCOPE,
                    kind="note",
                    idempotency_key="idem-rt-6",
                ),
            )
        )
        assert replaced["revision"] == 2
        fetched = data_of(
            service.get(ctx, models.GetRequest(memory_id=added["memory_id"], scope=SCOPE))
        )
        assert fetched["memory"]["content"] == "second draft"

    def test_stale_expected_revision_is_a_revision_conflict(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        added = data_of(add(service, ctx, content="conflict source", key="idem-rt-7"))
        error = error_of(
            service.replace(
                ctx,
                models.ReplaceRequest(
                    memory_id=added["memory_id"],
                    expected_revision=99,
                    content="never applied",
                    scope=SCOPE,
                    kind="note",
                    idempotency_key="idem-rt-8",
                ),
            )
        )
        assert error["code"] == models.ErrorCode.REVISION_CONFLICT.value
        assert error["details"]["current_revision"] == 1

    def test_remove_hides_the_memory_from_reads(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        added = data_of(add(service, ctx, content="soon deleted", key="idem-rt-9"))
        removed = data_of(
            service.remove(
                ctx,
                models.RemoveRequest(
                    memory_id=added["memory_id"],
                    scope=SCOPE,
                    expected_revision=1,
                    idempotency_key="idem-rt-10",
                ),
            )
        )
        assert removed["deleted"] is True
        assert removed["revision"] == 2

        assert (
            error_of(
                service.get(ctx, models.GetRequest(memory_id=added["memory_id"], scope=SCOPE))
            )["code"]
            == models.ErrorCode.NOT_FOUND.value
        )
        found = data_of(service.search(ctx, models.SearchRequest(query="deleted", scope=SCOPE)))
        assert found["memories"] == []

    def test_replayed_idempotency_key_returns_the_same_result(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        first = data_of(add(service, ctx, content="exactly once", key="idem-rt-11"))
        second = data_of(add(service, ctx, content="exactly once", key="idem-rt-11"))
        assert first == second
        assert rows(settings.db_path, "SELECT COUNT(*) FROM memory_metadata") == [(1,)]

    def test_same_key_different_payload_fails_closed(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content="original payload", key="idem-rt-12")
        error = error_of(add(service, ctx, content="different payload", key="idem-rt-12"))
        assert error["code"] == models.ErrorCode.IDEMPOTENCY_KEY_REUSED.value
        assert rows(settings.db_path, "SELECT COUNT(*) FROM memory_metadata") == [(1,)]

    def test_unauthorized_scope_is_refused_without_disclosure(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        error = error_of(
            service.search(ctx, models.SearchRequest(query="anything", scope=OTHER_SCOPE))
        )
        assert error["code"] == models.ErrorCode.NOT_AUTHORIZED.value

    def test_a_revoked_grant_can_no_longer_read(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        added = data_of(add(service, ctx, content="revocation canary", key="idem-rt-13"))

        conn = sqlite3.connect(settings.db_path)
        try:
            conn.execute(
                "UPDATE client_grants SET revoked_at = ? WHERE grant_id = ?",
                ("2026-08-04T01:00:00+00:00", grant.grant_id),
            )
            conn.commit()
        finally:
            conn.close()

        assert (
            error_of(
                service.get(ctx, models.GetRequest(memory_id=added["memory_id"], scope=SCOPE))
            )["code"]
            == models.ErrorCode.NOT_FOUND.value
        )
        assert (
            error_of(add(service, ctx, content="after revocation", key="idem-rt-14"))["code"]
            == models.ErrorCode.NOT_AUTHORIZED.value
        )

    def test_status_reports_health_without_a_path(self, tmp_path: Path) -> None:
        settings, service, grant = make_stack(tmp_path)
        ctx = grant.caller_context()
        add(service, ctx, content="status probe", key="idem-rt-15")
        status = data_of(service.status(ctx, models.StatusRequest()))
        assert status["database"]["reachable"] is True
        assert status["database"]["schema_version"] >= 1
        serialised = json.dumps(status)
        assert str(settings.db_path) not in serialised
        assert "authority.db" not in serialised

    def test_embedding_outage_never_writes_a_memory(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        service = authority.build_service(
            settings,
            embedder=StubEmbedder(available=False),
            env={},
            digest_key=b"deterministic-test-digest-key",
        )
        grant = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        error = error_of(
            add(service, grant.caller_context(), content="never stored", key="idem-rt-16")
        )
        assert error["code"] == models.ErrorCode.EMBEDDING_UNAVAILABLE.value
        assert rows(settings.db_path, "SELECT COUNT(*) FROM memory_metadata") == [(0,)]


# ---------------------------------------------------------------------------
# digest key
# ---------------------------------------------------------------------------


class TestDigestKey:
    def test_is_created_once_and_reused(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        first = authority.load_or_create_digest_key(config_dir, {})
        second = authority.load_or_create_digest_key(config_dir, {})
        assert first == second
        assert len(first) == 32

    def test_environment_override_wins(self, tmp_path: Path) -> None:
        key = authority.load_or_create_digest_key(
            tmp_path / "cfg", {authority.DIGEST_KEY_ENV: "0a0b0c0d"}
        )
        assert key == bytes.fromhex("0a0b0c0d")
        assert not (tmp_path / "cfg" / authority.DIGEST_KEY_FILE).exists()

    def test_a_corrupt_key_file_fails_closed(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / authority.DIGEST_KEY_FILE).write_text("not-hex\n", encoding="utf-8")
        with pytest.raises(authority.AuthorityError):
            authority.load_or_create_digest_key(config_dir, {})

    def test_the_key_never_lands_in_the_config_file(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        key = authority.load_or_create_digest_key(settings.config_dir, {})
        assert key.hex() not in settings.config_path.read_text(encoding="utf-8")

    def test_a_restarted_service_replays_the_same_idempotency_key(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        grant = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        ctx = grant.caller_context()

        first_service = authority.build_service(settings, embedder=StubEmbedder(), env={})
        first = data_of(add(first_service, ctx, content="survives restart", key="idem-restart-1"))

        # A brand-new process: same config dir, so the same persisted key.
        second_service = authority.build_service(settings, embedder=StubEmbedder(), env={})
        second = data_of(add(second_service, ctx, content="survives restart", key="idem-restart-1"))
        assert first == second


# ---------------------------------------------------------------------------
# caller resolution
# ---------------------------------------------------------------------------


class TestCallerResolution:
    def test_current_caller_without_a_request_is_not_authorized(self) -> None:
        with pytest.raises(repo.NotAuthorizedError):
            authority.current_caller()

    @pytest.mark.parametrize(
        "headers",
        [None, {}, {"authorization": "Basic abc"}, {"authorization": "Bearer "}],
    )
    def test_bearer_token_extraction_fails_closed(self, headers: Any) -> None:
        assert authority.bearer_token(headers) is None

    def test_bearer_token_is_case_insensitive_on_the_scheme(self) -> None:
        assert authority.bearer_token({"authorization": "bEaReR abc.def"}) == "abc.def"

    def test_static_resolver_returns_the_bound_grant(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)
        grant = authority.ensure_local_grant(settings.db_path, memory_scopes=(SCOPE,))
        resolve = authority.static_resolver(grant)
        caller = resolve(None)
        assert caller is not None
        assert caller.grant_id == grant.grant_id
        assert caller.memory_scopes == (SCOPE,)

    def test_token_resolver_rejects_a_missing_or_bogus_token(self, tmp_path: Path) -> None:
        from recall_memory_mcp.auth import SimpleTokenManager

        settings = initialised(tmp_path)
        manager = SimpleTokenManager(b"test-secret")
        resolve = authority.token_resolver(settings.db_path, token_manager=manager)
        assert resolve(None) is None
        assert resolve("not-a-token") is None

    def test_token_resolver_requires_a_stored_grant(self, tmp_path: Path) -> None:
        from recall_memory_mcp.auth import OAuthTokenPayload, SimpleTokenManager

        settings = initialised(tmp_path)
        manager = SimpleTokenManager(b"test-secret")
        token = manager.mint_token(
            OAuthTokenPayload(
                sub="subject-1",
                client_id="claude",
                scope="memory:read memory:write",
                exp=4_102_444_800,
                iss="https://issuer.example",
            )
        )
        resolve = authority.token_resolver(settings.db_path, token_manager=manager)
        assert resolve(token) is None

    def test_token_resolver_never_widens_the_stored_grant(self, tmp_path: Path) -> None:
        from recall_memory_mcp.auth import OAuthTokenPayload, SimpleTokenManager

        settings = initialised(tmp_path)
        owner_id = rows(settings.db_path, "SELECT owner_id FROM owners")[0][0]
        conn = sqlite3.connect(settings.db_path)
        try:
            conn.execute(
                """
                INSERT INTO client_grants (
                    grant_id, owner_id, issuer, subject, client_id, grant_generation,
                    grant_kind, source_client, oauth_scopes_json,
                    memory_scope_patterns_json, binding_challenge_id, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, 'OAUTH', ?, ?, ?, NULL, ?)
                """,
                (
                    "grant-claude-1",
                    owner_id,
                    "https://issuer.example",
                    "subject-1",
                    "claude",
                    "claude",
                    json.dumps(["memory:read"]),
                    json.dumps([SCOPE]),
                    "2026-08-04T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        manager = SimpleTokenManager(b"test-secret")
        token = manager.mint_token(
            OAuthTokenPayload(
                sub="subject-1",
                client_id="claude",
                # The token *claims* admin; the stored grant only allows read.
                scope="memory:read memory:write memory:admin",
                exp=4_102_444_800,
                iss="https://issuer.example",
            )
        )
        caller = authority.token_resolver(settings.db_path, token_manager=manager)(token)
        assert caller is not None
        assert caller.oauth_scopes == ("memory:read",)
        assert caller.memory_scopes == (SCOPE,)
        assert caller.grant_id == "grant-claude-1"

    def test_token_resolver_refuses_a_revoked_grant(self, tmp_path: Path) -> None:
        from recall_memory_mcp.auth import OAuthTokenPayload, SimpleTokenManager

        settings = initialised(tmp_path)
        owner_id = rows(settings.db_path, "SELECT owner_id FROM owners")[0][0]
        conn = sqlite3.connect(settings.db_path)
        try:
            conn.execute(
                """
                INSERT INTO client_grants (
                    grant_id, owner_id, issuer, subject, client_id, grant_generation,
                    grant_kind, source_client, oauth_scopes_json,
                    memory_scope_patterns_json, binding_challenge_id, created_at,
                    revoked_at
                ) VALUES (?, ?, ?, ?, ?, 1, 'OAUTH', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    "grant-revoked-1",
                    owner_id,
                    "https://issuer.example",
                    "subject-2",
                    "chatgpt",
                    "chatgpt",
                    json.dumps(["memory:read"]),
                    json.dumps([SCOPE]),
                    "2026-08-04T00:00:00+00:00",
                    "2026-08-04T00:10:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        manager = SimpleTokenManager(b"test-secret")
        token = manager.mint_token(
            OAuthTokenPayload(
                sub="subject-2",
                client_id="chatgpt",
                scope="memory:read",
                exp=4_102_444_800,
                iss="https://issuer.example",
            )
        )
        assert authority.token_resolver(settings.db_path, token_manager=manager)(token) is None


# ---------------------------------------------------------------------------
# build_service / build_app preflight
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_build_service_refuses_an_uninitialised_authority(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with pytest.raises(authority.AuthorityNotInitialisedError):
            authority.build_service(settings, embedder=StubEmbedder(), env={})

    def test_build_service_refuses_an_embedder_without_identity(self, tmp_path: Path) -> None:
        settings = initialised(tmp_path)

        class Anonymous:
            def encode(self, text: str) -> dict[int, bytes]:
                return {1: b"\x00\x00\x00\x00"}

        with pytest.raises(authority.AuthorityError):
            authority.build_service(settings, embedder=Anonymous(), env={})

    def test_build_app_refuses_unauthenticated_non_loopback(self, tmp_path: Path) -> None:
        settings = initialised(
            tmp_path,
            RECALL_MCP_MODE="tunnel-dev",
            RECALL_MCP_HOST="0.0.0.0",
            RECALL_MCP_ALLOWED_HOSTS="example.test",
            RECALL_MCP_REQUIRE_AUTH="false",
        )
        with pytest.raises(authority.AuthorityError):
            authority.build_app(settings, embedder=StubEmbedder(), env={})

    def test_build_app_requires_a_token_secret_when_auth_is_on(self, tmp_path: Path) -> None:
        settings = initialised(
            tmp_path,
            RECALL_MCP_MODE="tunnel-dev",
            RECALL_MCP_HOST="127.0.0.1",
            RECALL_MCP_ALLOWED_HOSTS="127.0.0.1:19881",
        )
        with pytest.raises(authority.AuthorityError):
            authority.build_app(settings, embedder=StubEmbedder(), env={})
