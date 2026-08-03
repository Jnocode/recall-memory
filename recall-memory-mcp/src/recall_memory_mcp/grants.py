"""Client Grant Manager, Account Linking, and Pairing Challenges (Tasks 5.6a - 5.6d)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

from .auth import AuthError, OAuthTokenPayload

logger = logging.getLogger("recall_memory_mcp.grants")


class PairingError(AuthError):
    """Raised when pairing challenge verification fails."""


class ChallengeExpiredError(PairingError):
    """Raised when pairing challenge TTL has expired."""


class ChallengeUsedError(PairingError):
    """Raised when pairing challenge is replayed."""


@dataclass(frozen=True)
class ClientGrant:
    grant_id: str
    owner_id: str
    issuer: str
    subject: str
    client_id: str
    generation: int
    scopes: tuple[str, ...]
    created_at: int
    active: bool = True


@dataclass(frozen=True)
class OwnerLinkEvent:
    event_id: str
    owner_id: str
    action: str  # initial_bind, reauthorize, scope_change, revoke, unlink
    grant_id: str
    issuer: str
    subject: str
    client_id: str
    generation: int
    timestamp: int


class ClientGrantManager:
    """Manages Client Grants, Account Linking, Audit Events and Pairing Challenges."""

    def __init__(self, secret: bytes) -> None:
        self.secret = secret
        self.grants: dict[str, ClientGrant] = {}  # grant_id -> ClientGrant
        self.link_events: list[OwnerLinkEvent] = []
        self._used_challenges: set[str] = set()

    def create_pairing_challenge(
        self, owner_id: str, client_id: str, state: str, code_challenge: str, ttl_seconds: int = 300, now: int | None = None
    ) -> str:
        current_time = int(time.time()) if now is None else now
        exp = current_time + ttl_seconds
        payload = f"{owner_id}:{client_id}:{state}:{code_challenge}:{exp}"
        sig = hmac.new(self.secret, payload.encode("utf-8"), "sha256").hexdigest()
        challenge_token = f"{payload}:{sig}"
        return challenge_token

    def verify_and_claim_challenge(
        self,
        challenge_token: str,
        expected_owner_id: str,
        expected_client_id: str,
        state: str,
        code_verifier: str,
        now: int | None = None,
    ) -> bool:
        if challenge_token in self._used_challenges:
            raise ChallengeUsedError("Pairing challenge has already been used")

        parts = challenge_token.rsplit(":", 1)
        if len(parts) != 2:
            raise PairingError("Malformed challenge token")

        payload, sig = parts[0], parts[1]
        expected_sig = hmac.new(self.secret, payload.encode("utf-8"), "sha256").hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            raise PairingError("Invalid challenge signature")

        owner_id, client_id, req_state, code_challenge, exp_str = payload.split(":")
        current_time = int(time.time()) if now is None else now

        if int(exp_str) < current_time:
            raise ChallengeExpiredError("Pairing challenge expired")

        if owner_id != expected_owner_id or client_id != expected_client_id or req_state != state:
            raise PairingError("Challenge parameters mismatch")

        # Verify PKCE S256
        verifier_hash = hashlib.sha256(code_verifier.encode("utf-8")).hexdigest()
        if verifier_hash != code_challenge:
            raise PairingError("PKCE verification failed")

        self._used_challenges.add(challenge_token)
        return True

    def register_or_update_grant(
        self,
        owner_id: str,
        issuer: str,
        subject: str,
        client_id: str,
        scopes: tuple[str, ...],
        now: int | None = None,
    ) -> ClientGrant:
        grant_key = f"{issuer}:{subject}:{client_id}"
        existing = self.grants.get(grant_key)
        current_time = int(time.time()) if now is None else now

        if existing:
            # Account linking constraint (R5.6a): owner_id cannot change without admin approval
            if existing.owner_id != owner_id:
                raise AuthError("Cross-issuer account linking collision fail-closed")
            generation = existing.generation + 1
            action = "scope_change" if existing.scopes != scopes else "reauthorize"
        else:
            generation = 1
            action = "initial_bind"

        grant = ClientGrant(
            grant_id=f"grant-{grant_key}",
            owner_id=owner_id,
            issuer=issuer,
            subject=subject,
            client_id=client_id,
            generation=generation,
            scopes=scopes,
            created_at=current_time,
            active=True,
        )
        self.grants[grant_key] = grant

        # Audit event (R5.6c)
        event = OwnerLinkEvent(
            event_id=f"evt-{len(self.link_events)+1}",
            owner_id=owner_id,
            action=action,
            grant_id=grant.grant_id,
            issuer=issuer,
            subject=subject,
            client_id=client_id,
            generation=generation,
            timestamp=current_time,
        )
        self.link_events.append(event)
        return grant

    def revoke_grant(self, issuer: str, subject: str, client_id: str, now: int | None = None) -> None:
        grant_key = f"{issuer}:{subject}:{client_id}"
        grant = self.grants.get(grant_key)
        if grant and grant.active:
            current_time = int(time.time()) if now is None else now
            updated = ClientGrant(
                grant_id=grant.grant_id,
                owner_id=grant.owner_id,
                issuer=grant.issuer,
                subject=grant.subject,
                client_id=grant.client_id,
                generation=grant.generation + 1,
                scopes=grant.scopes,
                created_at=grant.created_at,
                active=False,
            )
            self.grants[grant_key] = updated
            self.link_events.append(
                OwnerLinkEvent(
                    event_id=f"evt-{len(self.link_events)+1}",
                    owner_id=grant.owner_id,
                    action="revoke",
                    grant_id=grant.grant_id,
                    issuer=issuer,
                    subject=subject,
                    client_id=client_id,
                    generation=updated.generation,
                    timestamp=current_time,
                )
            )
