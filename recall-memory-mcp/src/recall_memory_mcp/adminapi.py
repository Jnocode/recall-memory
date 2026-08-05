"""Online authority admin API + OS-bound local admin session (task 6.10a).

Design constraint that shapes this whole module: ``memory export``,
``memory restore`` and ``memory purge`` have **exactly one** implementation
path — an HTTP call into the *running* authority process.  There is no
offline twin, no ``--db-path`` shortcut and no ``--owner-id`` flag, because
either of those would be a second, ACL-free way to reach the same rows
(R8.10, design §9.1).

Two things may authenticate an admin call, and both are resolved *here*, on
the server:

1. **An OS-bound local admin session.**  When the authority starts it writes
   a single-file session (owner-only permissions, recorded OS account,
   expiry, and the endpoint it is listening on).  Possessing the token is
   proof of being the same OS account that owns the authority, because the
   OS refuses the read to anyone else.  The session file therefore *is* the
   binding — nothing in the request can assert an identity.
2. **A verified ``memory:admin`` OAuth grant**, resolved through the same
   resolver the MCP endpoint uses.

Either way the owner comes from the stored grant.  A request never names an
owner, and a caller that is neither of the above gets ``401``.
"""

from __future__ import annotations

import getpass
import hmac
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from . import admin as admin_mod
from . import provisioning, redaction
from .service import SCOPE_ADMIN, CallerContext

logger = logging.getLogger("recall_memory_mcp.adminapi")

ADMIN_PREFIX: Final[str] = "/admin/v1"
EXPORT_PATH: Final[str] = f"{ADMIN_PREFIX}/memory/export"
RESTORE_PATH: Final[str] = f"{ADMIN_PREFIX}/memory/restore"
PURGE_PATH: Final[str] = f"{ADMIN_PREFIX}/memory/purge"

SESSION_FILE_NAME: Final[str] = "local-admin-session.json"
SESSION_TTL_SECONDS: Final[int] = 12 * 3600
SESSION_SCHEMA_VERSION: Final[int] = 1

LOOPBACK_CLIENTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


class AdminSessionError(RuntimeError):
    """The local admin session is missing, expired or not ours."""


# ---------------------------------------------------------------------------
# OS-bound local admin session
# ---------------------------------------------------------------------------


def session_path_for(config_dir: str | Path) -> Path:
    return Path(config_dir).expanduser() / SESSION_FILE_NAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _os_account() -> str:
    """Best available identity of the OS account running this process."""

    try:
        return f"uid:{os.getuid()}"  # type: ignore[attr-defined]
    except AttributeError:  # Windows has no getuid
        try:
            return f"user:{getpass.getuser()}"
        except Exception:  # noqa: BLE001 - never fail a startup over a name lookup
            return "user:unknown"


@dataclass(frozen=True)
class LocalAdminSession:
    """A short-lived, owner-only, on-disk admin session."""

    token: str
    os_account: str
    endpoint: str
    created_at: str
    expires_at: str
    path: Path

    def is_expired(self, *, now: datetime | None = None) -> bool:
        moment = now or _utc_now()
        return moment >= datetime.fromisoformat(self.expires_at)

    def redacted(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "endpoint": self.endpoint,
            "expires_at": self.expires_at,
            "os_account": self.os_account,
            "token": redaction.REDACTED,
        }


def create_local_admin_session(
    config_dir: str | Path,
    *,
    endpoint: str,
    ttl_seconds: int = SESSION_TTL_SECONDS,
    now: datetime | None = None,
) -> LocalAdminSession:
    """Write a fresh owner-only session file, replacing any previous one."""

    moment = now or _utc_now()
    path = session_path_for(config_dir)
    if path.is_symlink():
        raise AdminSessionError(
            "the admin session path is a symbolic link; refusing to write a session"
        )
    session = LocalAdminSession(
        token=secrets.token_urlsafe(32),
        os_account=_os_account(),
        endpoint=endpoint,
        created_at=moment.isoformat(timespec="seconds"),
        expires_at=(moment + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        path=path,
    )
    payload = {
        "created_at": session.created_at,
        "endpoint": session.endpoint,
        "expires_at": session.expires_at,
        "os_account": session.os_account,
        "schema_version": SESSION_SCHEMA_VERSION,
        "token": session.token,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    provisioning.harden_file(path)
    return session


def load_local_admin_session(config_dir: str | Path) -> LocalAdminSession:
    """Read the session back, proving it belongs to *this* OS account."""

    path = session_path_for(config_dir)
    if path.is_symlink():
        raise AdminSessionError("the admin session path is a symbolic link")
    if not path.is_file():
        raise AdminSessionError(
            "no local admin session; the authority is not running. "
            "Admin commands have no offline path - start `serve` first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdminSessionError("the local admin session file is unreadable") from exc
    try:
        session = LocalAdminSession(
            token=str(payload["token"]),
            os_account=str(payload["os_account"]),
            endpoint=str(payload["endpoint"]),
            created_at=str(payload["created_at"]),
            expires_at=str(payload["expires_at"]),
            path=path,
        )
    except KeyError as exc:
        raise AdminSessionError("the local admin session file is incomplete") from exc

    # OS binding: the session belongs to one account, and on POSIX the file
    # itself must be owned by us and unreadable by anybody else.
    if session.os_account != _os_account():
        raise AdminSessionError(
            "the local admin session belongs to a different OS account"
        )
    if hasattr(os, "getuid"):
        stat = path.stat()
        if stat.st_uid != os.getuid():  # type: ignore[attr-defined]
            raise AdminSessionError("the local admin session file is not owned by this account")
        if stat.st_mode & 0o077:
            raise AdminSessionError(
                "the local admin session file is readable by other accounts"
            )
    if session.is_expired():
        raise AdminSessionError("the local admin session has expired; restart the authority")
    return session


def discard_local_admin_session(config_dir: str | Path) -> bool:
    """Remove the session file (best effort; a crash simply leaves it stale)."""

    path = session_path_for(config_dir)
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
            return True
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("could not discard admin session: %s", redaction.redact_exception(exc))
    return False


# ---------------------------------------------------------------------------
# request authentication
# ---------------------------------------------------------------------------


def _peer_is_loopback(request: Any) -> bool:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host is None:
        return False
    return str(host) in LOOPBACK_CLIENTS


def _bearer(request: Any) -> str | None:
    raw = request.headers.get("authorization") or request.headers.get("Authorization")
    if not raw:
        return None
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


@dataclass
class AdminAuthenticator:
    """Resolves an admin caller, or nothing at all.

    ``local_context_factory`` is only consulted for a token that matches the
    live session, so a stale or forged token can never mint a local admin.
    """

    session: LocalAdminSession | None
    local_context_factory: Callable[[], CallerContext] | None = None
    oauth_resolver: Callable[[str | None], CallerContext | None] | None = None

    def resolve(self, request: Any) -> tuple[CallerContext | None, str]:
        if not _peer_is_loopback(request):
            return None, "non_loopback"
        token = _bearer(request)
        if not token:
            return None, "missing_token"
        session = self.session
        if (
            session is not None
            and self.local_context_factory is not None
            and not session.is_expired()
            and hmac.compare_digest(token, session.token)
        ):
            try:
                return self.local_context_factory(), "local_admin_session"
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "local admin context unavailable: %s", redaction.redact_exception(exc)
                )
                return None, "unavailable"
        if self.oauth_resolver is not None:
            try:
                ctx = self.oauth_resolver(token)
            except Exception as exc:  # noqa: BLE001 - uniform denial
                logger.info("admin token rejected: %s", redaction.redact_exception(exc))
                ctx = None
            if ctx is not None and ctx.has_oauth_scope(SCOPE_ADMIN):
                return ctx, "oauth_grant"
            # A valid non-admin token is still not an admin caller.
            return None, "insufficient_grant"
        return None, "unauthenticated"


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def _json_response(payload: Mapping[str, Any], status: int) -> Any:
    from starlette.responses import JSONResponse  # noqa: PLC0415

    return JSONResponse(dict(payload), status_code=status)


def _unauthorized(reason: str) -> Any:
    if reason in {"non_loopback", "insufficient_grant"}:
        return _json_response({"error": "not_authorized"}, 403)
    return _json_response(
        {"error": "unauthorized"},
        401,
    )


def _error_response(exc: Exception) -> Any:
    if isinstance(exc, admin_mod.AdminNotAuthorizedError):
        return _json_response({"error": "not_authorized"}, 403)
    if isinstance(exc, admin_mod.AdminRevisionConflictError):
        return _json_response(
            {"error": "revision_conflict", "current_revision": exc.current_revision}, 409
        )
    if isinstance(exc, admin_mod.AdminValidationError):
        return _json_response({"error": "validation_error"}, 400)
    if isinstance(exc, admin_mod.AdminUnavailableError):
        return _json_response({"error": "unavailable"}, 503)
    logger.error("admin endpoint failure: %s", redaction.redact_exception(exc))
    return _json_response({"error": "internal_error"}, 500)


async def _payload(request: Any) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise admin_mod.AdminValidationError("admin requests must be application/json")
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise admin_mod.AdminValidationError("admin request body must be JSON") from exc
    if not isinstance(body, dict):
        raise admin_mod.AdminValidationError("admin request body must be a JSON object")
    if "owner_id" in body or "grant_id" in body:
        # R8.10 — the owner is derived from the verified session/grant. A
        # request that tries to name one is refused outright rather than
        # silently ignored, so the contract is impossible to misread.
        raise admin_mod.AdminValidationError(
            "owner_id/grant_id are never accepted from a request"
        )
    return body


def build_admin_routes(
    admin_service: admin_mod.AdminService,
    authenticator: AdminAuthenticator,
) -> tuple[tuple[str, tuple[str, ...], Any], ...]:
    """``(path, methods, endpoint)`` triples for the authority app."""

    async def export_endpoint(request: Any) -> Any:
        ctx, reason = authenticator.resolve(request)
        if ctx is None:
            return _unauthorized(reason)
        try:
            body = await _payload(request)
            result = admin_service.export(
                ctx,
                scope=body.get("scope"),
                include_tombstones=bool(body.get("include_tombstones", False)),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        payload = result.document()
        payload["audit_event_id"] = result.audit_event_id
        payload["auth"] = reason
        return _json_response(payload, 200)

    async def restore_endpoint(request: Any) -> Any:
        ctx, reason = authenticator.resolve(request)
        if ctx is None:
            return _unauthorized(reason)
        try:
            body = await _payload(request)
            result = admin_service.restore(
                ctx,
                memory_id=body.get("memory_id"),
                scope=body.get("scope"),
                expected_revision=body.get("expected_revision"),
                idempotency_key=body.get("idempotency_key"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        payload = result.as_json()
        payload["auth"] = reason
        return _json_response(payload, 200)

    async def purge_endpoint(request: Any) -> Any:
        ctx, reason = authenticator.resolve(request)
        if ctx is None:
            return _unauthorized(reason)
        try:
            body = await _payload(request)
            result = admin_service.purge(
                ctx,
                memory_id=body.get("memory_id"),
                scope=body.get("scope"),
                expected_revision=body.get("expected_revision"),
                idempotency_key=body.get("idempotency_key"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        payload = result.as_json()
        payload["auth"] = reason
        payload["backup_retention_notice"] = admin_mod.BACKUP_RETENTION_NOTICE
        return _json_response(payload, 200)

    return (
        (EXPORT_PATH, ("POST",), export_endpoint),
        (RESTORE_PATH, ("POST",), restore_endpoint),
        (PURGE_PATH, ("POST",), purge_endpoint),
    )


__all__ = [
    "ADMIN_PREFIX",
    "EXPORT_PATH",
    "LOOPBACK_CLIENTS",
    "PURGE_PATH",
    "RESTORE_PATH",
    "SESSION_FILE_NAME",
    "SESSION_TTL_SECONDS",
    "AdminAuthenticator",
    "AdminSessionError",
    "LocalAdminSession",
    "build_admin_routes",
    "create_local_admin_session",
    "discard_local_admin_session",
    "load_local_admin_session",
    "session_path_for",
]
