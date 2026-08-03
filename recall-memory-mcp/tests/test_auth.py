"""Unit and Integration tests for OAuth 2.0 / 2.1 authentication and scope enforcement (Task 5.8)."""

from __future__ import annotations

import hashlib
import json
import pytest

from recall_memory_mcp.auth import (
    DEFAULT_AUDIENCE,
    InsufficientScopeError,
    InvalidTokenError,
    OAuthTokenPayload,
    SimpleTokenManager,
    TokenExpiredError,
    TokenRevokedError,
    payload_to_caller_context,
)
from recall_memory_mcp.grants import (
    ChallengeExpiredError,
    ChallengeUsedError,
    ClientGrantManager,
    PairingError,
)

SECRET = b"test-secret-key-32-bytes-long!!!"


@pytest.fixture
def token_manager() -> SimpleTokenManager:
    return SimpleTokenManager(secret=SECRET)


@pytest.fixture
def grant_manager() -> ClientGrantManager:
    return ClientGrantManager(secret=SECRET)


class TestOAuthTokenValidation:
    def test_valid_token_mint_and_parse(self, token_manager: SimpleTokenManager) -> None:
        payload = OAuthTokenPayload(
            sub="user-123",
            client_id="client-chatgpt",
            scope="memory:read memory:write",
            exp=2000000000,
        )
        token = token_manager.mint_token(payload)
        parsed = token_manager.validate_token(token, now=1000000000)

        assert parsed.sub == "user-123"
        assert parsed.client_id == "client-chatgpt"
        assert "memory:read" in parsed.scopes
        assert "memory:write" in parsed.scopes

    def test_expired_token_raises_token_expired_error(self, token_manager: SimpleTokenManager) -> None:
        payload = OAuthTokenPayload(
            sub="user-123",
            client_id="client-claude",
            scope="memory:read",
            exp=1000,
        )
        token = token_manager.mint_token(payload)
        with pytest.raises(TokenExpiredError):
            token_manager.validate_token(token, now=2000)

    def test_wrong_audience_raises_invalid_token_error(self, token_manager: SimpleTokenManager) -> None:
        payload = OAuthTokenPayload(
            sub="user-123",
            client_id="client-kiro",
            scope="memory:read",
            exp=2000000000,
            aud="wrong-audience",
        )
        token = token_manager.mint_token(payload)
        with pytest.raises(InvalidTokenError):
            token_manager.validate_token(token, now=1000000000)

    def test_revoked_token_raises_token_revoked_error(self, token_manager: SimpleTokenManager) -> None:
        payload = OAuthTokenPayload(
            sub="user-123",
            client_id="client-kiro",
            scope="memory:read",
            exp=2000000000,
        )
        token = token_manager.mint_token(payload)
        token_manager.revoke_token(token)
        with pytest.raises(TokenRevokedError):
            token_manager.validate_token(token, now=1000000000)

    def test_revoked_generation_raises_token_revoked_error(self, token_manager: SimpleTokenManager) -> None:
        payload = OAuthTokenPayload(
            sub="user-123",
            client_id="client-kiro",
            scope="memory:read",
            exp=2000000000,
            generation=1,
        )
        token = token_manager.mint_token(payload)
        token_manager.revoke_generation("client-kiro", 1)
        with pytest.raises(TokenRevokedError):
            token_manager.validate_token(token, now=1000000000)


class TestPairingAndAccountLinking:
    def test_successful_pairing_challenge(self, grant_manager: ClientGrantManager) -> None:
        verifier = "secret-pkce-verifier-123"
        challenge = hashlib.sha256(verifier.encode("utf-8")).hexdigest()

        token = grant_manager.create_pairing_challenge(
            owner_id="owner-1",
            client_id="client-kiro",
            state="state-abc",
            code_challenge=challenge,
            ttl_seconds=300,
            now=1000,
        )

        res = grant_manager.verify_and_claim_challenge(
            challenge_token=token,
            expected_owner_id="owner-1",
            expected_client_id="client-kiro",
            state="state-abc",
            code_verifier=verifier,
            now=1100,
        )
        assert res is True

    def test_replayed_pairing_challenge_fails(self, grant_manager: ClientGrantManager) -> None:
        verifier = "secret-pkce-verifier-123"
        challenge = hashlib.sha256(verifier.encode("utf-8")).hexdigest()

        token = grant_manager.create_pairing_challenge(
            owner_id="owner-1",
            client_id="client-kiro",
            state="state-abc",
            code_challenge=challenge,
            ttl_seconds=300,
            now=1000,
        )

        grant_manager.verify_and_claim_challenge(
            challenge_token=token,
            expected_owner_id="owner-1",
            expected_client_id="client-kiro",
            state="state-abc",
            code_verifier=verifier,
            now=1100,
        )

        with pytest.raises(ChallengeUsedError):
            grant_manager.verify_and_claim_challenge(
                challenge_token=token,
                expected_owner_id="owner-1",
                expected_client_id="client-kiro",
                state="state-abc",
                code_verifier=verifier,
                now=1105,
            )

    def test_expired_pairing_challenge_fails(self, grant_manager: ClientGrantManager) -> None:
        verifier = "secret-pkce-verifier-123"
        challenge = hashlib.sha256(verifier.encode("utf-8")).hexdigest()

        token = grant_manager.create_pairing_challenge(
            owner_id="owner-1",
            client_id="client-kiro",
            state="state-abc",
            code_challenge=challenge,
            ttl_seconds=300,
            now=1000,
        )

        with pytest.raises(ChallengeExpiredError):
            grant_manager.verify_and_claim_challenge(
                challenge_token=token,
                expected_owner_id="owner-1",
                expected_client_id="client-kiro",
                state="state-abc",
                code_verifier=verifier,
                now=1400,
            )

    def test_grant_audit_events_and_revocation(self, grant_manager: ClientGrantManager) -> None:
        grant = grant_manager.register_or_update_grant(
            owner_id="owner-1",
            issuer="https://auth.recall.local",
            subject="sub-user-1",
            client_id="chatgpt",
            scopes=("memory:read", "memory:write"),
            now=1000,
        )
        assert grant.generation == 1
        assert grant.active is True
        assert len(grant_manager.link_events) == 1
        assert grant_manager.link_events[0].action == "initial_bind"

        grant_manager.revoke_grant(
            issuer="https://auth.recall.local",
            subject="sub-user-1",
            client_id="chatgpt",
            now=1100,
        )
        assert len(grant_manager.link_events) == 2
        assert grant_manager.link_events[1].action == "revoke"
        revoked_grant = grant_manager.grants["https://auth.recall.local:sub-user-1:chatgpt"]
        assert revoked_grant.active is False
