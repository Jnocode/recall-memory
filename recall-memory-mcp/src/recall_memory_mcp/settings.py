"""Server settings (task 2.5).

Defaults are loopback-only (R9.3), never carry a secret (R7.5), never point
at the user's private Recall database, and every dump/repr is redacted
(R8.5).  Settings are resolved from an explicit environment mapping so tests
never depend on the machine that runs them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any, Final

from . import redaction

APP_DIR_NAME: Final[str] = "recall-memory-mcp"
DB_FILE_NAME: Final[str] = "authority.db"
CONFIG_FILE_NAME: Final[str] = "config.toml"

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8765
DEFAULT_MAX_BODY_BYTES: Final[int] = 1 << 20  # 1 MiB
DEFAULT_TOOL_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_MAX_CONCURRENT_CALLS: Final[int] = 8

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

ENV_PREFIX: Final[str] = "RECALL_MCP_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class SettingsError(ValueError):
    """Configuration is unsafe or malformed; the server must not start."""


class ServerMode(str, Enum):
    LOCAL = "local"
    TUNNEL_DEV = "tunnel-dev"
    REMOTE = "remote"


def _get(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(ENV_PREFIX + name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{ENV_PREFIX}{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise SettingsError(f"{ENV_PREFIX}{name} must be between {minimum} and {maximum}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{ENV_PREFIX}{name} must be a number") from exc
    if value <= minimum:
        raise SettingsError(f"{ENV_PREFIX}{name} must be greater than {minimum}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise SettingsError(f"{ENV_PREFIX}{name} must be a boolean")


def _csv(env: Mapping[str, str], name: str) -> tuple[str, ...] | None:
    raw = _get(env, name)
    if raw is None:
        return None
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        raise SettingsError(f"{ENV_PREFIX}{name} must not be empty")
    if any(item == "*" for item in items):
        # R7.1 / design 6.4 — a wildcard allowlist is never acceptable.
        raise SettingsError(f"{ENV_PREFIX}{name} must not contain a wildcard")
    return items


def _default_config_dir(env: Mapping[str, str]) -> Path:
    appdata = env.get("APPDATA")
    if appdata and appdata.strip():
        return Path(appdata.strip()) / APP_DIR_NAME
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and xdg.strip():
        return Path(xdg.strip()) / APP_DIR_NAME
    home = env.get("HOME") or env.get("USERPROFILE") or "."
    return Path(home) / ".config" / APP_DIR_NAME


@dataclass(frozen=True, repr=False)
class ServerSettings:
    """Immutable, redaction-aware server configuration."""

    mode: ServerMode
    host: str
    port: int
    config_dir: Path
    config_path: Path
    db_path: Path
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    require_auth: bool
    public_url: str | None = None
    oauth_issuer: str | None = None
    oauth_client_secret: str | None = None
    static_api_key: str | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS
    #: MVP contract is stateful Streamable HTTP (design 6.2). Not switchable.
    stateful_http: bool = True

    # -- construction ---------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        env = dict(os.environ) if env is None else env

        raw_mode = _get(env, "MODE") or ServerMode.LOCAL.value
        try:
            mode = ServerMode(raw_mode)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in ServerMode)
            raise SettingsError(f"{ENV_PREFIX}MODE must be one of: {allowed}") from exc

        host = _get(env, "HOST") or DEFAULT_HOST
        port = _int(env, "PORT", DEFAULT_PORT, minimum=1, maximum=65535)

        config_dir_raw = _get(env, "CONFIG_DIR")
        config_dir = Path(config_dir_raw) if config_dir_raw else _default_config_dir(env)
        db_raw = _get(env, "DB_PATH")
        db_path = Path(db_raw) if db_raw else config_dir / DB_FILE_NAME

        public_url = _get(env, "PUBLIC_URL")
        oauth_issuer = _get(env, "OAUTH_ISSUER")
        require_auth = _bool(env, "REQUIRE_AUTH", mode is not ServerMode.LOCAL)

        allowed_hosts = _csv(env, "ALLOWED_HOSTS")
        allowed_origins = _csv(env, "ALLOWED_ORIGINS")

        if mode is ServerMode.LOCAL:
            if host not in LOOPBACK_HOSTS:
                raise SettingsError("local mode must bind a loopback address")
            allowed_hosts = allowed_hosts or (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
                "127.0.0.1",
                "localhost",
            )
            allowed_origins = allowed_origins or (
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            )
        else:
            if not allowed_hosts:
                raise SettingsError(f"{mode.value} mode requires an explicit host allowlist")
            allowed_origins = allowed_origins or allowed_hosts

        if mode is ServerMode.REMOTE:
            # R1.4 / R7.2 — public exposure demands HTTPS + OAuth, no exceptions.
            if not public_url or not public_url.lower().startswith("https://"):
                raise SettingsError("remote mode requires an https public URL")
            if not oauth_issuer:
                raise SettingsError("remote mode requires an OAuth issuer")
            if not require_auth:
                raise SettingsError("remote mode cannot disable authentication")

        settings = cls(
            mode=mode,
            host=host,
            port=port,
            config_dir=config_dir,
            config_path=config_dir / CONFIG_FILE_NAME,
            db_path=db_path,
            allowed_hosts=tuple(allowed_hosts),
            allowed_origins=tuple(allowed_origins),
            require_auth=require_auth,
            public_url=public_url,
            oauth_issuer=oauth_issuer,
            oauth_client_secret=_get(env, "OAUTH_CLIENT_SECRET"),
            static_api_key=_get(env, "STATIC_API_KEY"),
            max_body_bytes=_int(
                env, "MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, minimum=1, maximum=1 << 28
            ),
            tool_timeout_seconds=_float(
                env, "TOOL_TIMEOUT_SECONDS", DEFAULT_TOOL_TIMEOUT_SECONDS, minimum=0.0
            ),
            max_concurrent_calls=_int(
                env, "MAX_CONCURRENT_CALLS", DEFAULT_MAX_CONCURRENT_CALLS, minimum=1, maximum=1024
            ),
        )
        return settings

    # -- safe output ----------------------------------------------------
    _SECRET_FIELDS = ("oauth_client_secret", "static_api_key")
    _PATH_FIELDS = ("config_dir", "config_path", "db_path")

    def redacted_dump(self) -> dict[str, Any]:
        """A dict that is safe to log, print or return from ``doctor``."""

        dumped: dict[str, Any] = {}
        for field in fields(self):
            name = field.name
            value = getattr(self, name)
            if name in self._PATH_FIELDS:
                dumped[name] = redaction.redact_path(value)
            elif name in self._SECRET_FIELDS:
                dumped[name] = None if value is None else redaction.REDACTED
            elif isinstance(value, ServerMode):
                dumped[name] = value.value
            elif isinstance(value, tuple):
                dumped[name] = list(value)
            elif isinstance(value, str):
                dumped[name] = redaction.redact_text(value)
            else:
                dumped[name] = value
        return dumped

    def __repr__(self) -> str:
        inner = ", ".join(f"{key}={value!r}" for key, value in self.redacted_dump().items())
        return f"{type(self).__name__}({inner})"

    __str__ = __repr__


__all__ = [
    "APP_DIR_NAME",
    "CONFIG_FILE_NAME",
    "DB_FILE_NAME",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENV_PREFIX",
    "LOOPBACK_HOSTS",
    "ServerMode",
    "ServerSettings",
    "SettingsError",
]
