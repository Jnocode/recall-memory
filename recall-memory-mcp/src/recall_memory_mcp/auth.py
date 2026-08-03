"""Authentication, Token Validation and OAuth Security for recall-memory-mcp (Phase 5)."""

from __future__ import annotations

import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

from . import redaction
from .service import CallerContext, SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE

logger = logging.getLogger("recall_memory_mcp.auth")

DEFAULT_AUDIENCE: Final[str] = "recall-memory-mcp"


class AuthError(PermissionError):
    """Base exception for authentication and scope failures."""


class TokenExpiredError(AuthError):
    """Raised when a token's exp timestamp has passed."""


class TokenRevokedError(AuthError):
    """Raised when a token or grant generation has been revoked."""


class InsufficientScopeError(AuthError):
    """Raised when caller context lacks required OAuth scope."""


class InvalidTokenError(AuthError):
    """Raised when token signature, audience, or payload format is invalid."""


@dataclass(frozen=True)
class OAuthTokenPayload:
    sub: str
    client_id: str
    scope: str
    exp: int
    aud: str = DEFAULT_AUDIENCE
    iss: str = "https://auth.recall.local"
    generation: int = 1
    client_type: str = "pre-registered"  # CIMD, DCR, or pre-registered

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.scope.split() if s.strip())


class SimpleTokenManager:
    """HMAC-signed token manager for authentication testing & local deployment."""

    def __init__(self, secret: bytes, audience: str = DEFAULT_AUDIENCE) -> None:
        self.secret = secret
        self.audience = audience
        self._revoked_tokens: set[str] = set()
        self._revoked_generations: set[tuple[str, int]] = set()  # (client_id, generation)

    def mint_token(self, payload: OAuthTokenPayload) -> str:
        data = {
            "sub": payload.sub,
            "client_id": payload.client_id,
            "scope": payload.scope,
            "exp": payload.exp,
            "aud": payload.aud,
            "iss": payload.iss,
            "generation": payload.generation,
            "client_type": payload.client_type,
        }
        encoded_body = json.dumps(data, sort_keys=True).encode("utf-8")
        sig = hmac.new(self.secret, encoded_body, "sha256").hexdigest()
        token = f"{encoded_body.hex()}.{sig}"
        return token

    def revoke_token(self, token: str) -> None:
        self._revoked_tokens.add(token)

    def revoke_generation(self, client_id: str, generation: int) -> None:
        self._revoked_generations.add((client_id, generation))

    def validate_token(self, token: str, now: int | None = None) -> OAuthTokenPayload:
        if token in self._revoked_tokens:
            raise TokenRevokedError("Token has been explicitly revoked")

        try:
            body_hex, sig = token.rsplit(".", 1)
            encoded_body = bytes.fromhex(body_hex)
            expected_sig = hmac.new(self.secret, encoded_body, "sha256").hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                raise InvalidTokenError("Invalid token signature")
            data = json.loads(encoded_body.decode("utf-8"))
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            logger.warning("Token validation failed: %s", redaction.redact_exception(exc))
            raise InvalidTokenError("Malformed or unparseable token") from exc

        current_time = int(time.time()) if now is None else now
        if data.get("exp", 0) < current_time:
            raise TokenExpiredError("Token has expired")

        if data.get("aud") != self.audience:
            raise InvalidTokenError(f"Invalid audience: {data.get('aud')}")

        payload = OAuthTokenPayload(
            sub=data["sub"],
            client_id=data["client_id"],
            scope=data["scope"],
            exp=data["exp"],
            aud=data.get("aud", self.audience),
            iss=data.get("iss", "https://auth.recall.local"),
            generation=data.get("generation", 1),
            client_type=data.get("client_type", "pre-registered"),
        )

        if (payload.client_id, payload.generation) in self._revoked_generations:
            raise TokenRevokedError("Token generation has been revoked")

        return payload


def payload_to_caller_context(
    payload: OAuthTokenPayload,
    grant_id: str,
    owner_id: str,
    memory_scopes: tuple[str, ...] = ("project:recall", "global"),
) -> CallerContext:
    """Map OAuthTokenPayload to immutable CallerContext (R5.6)."""
    return CallerContext(
        grant_id=grant_id,
        owner_id=owner_id,
        actor_id=f"actor:{payload.sub}",
        source_client=payload.client_id,
        oauth_scopes=payload.scopes,
        memory_scopes=memory_scopes,
    )
