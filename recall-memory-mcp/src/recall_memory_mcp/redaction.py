"""Redaction helpers (task 2.6).

R8.5: logs and errors SHALL be redacted — no absolute DB paths, no
tracebacks, no tokens, no credential-bearing URLs.

Everything here is pure text processing: no I/O, no logging side effects,
so it is safe to call from an error path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

REDACTED: Final[str] = "[REDACTED]"

#: Mapping keys whose *value* is always dropped regardless of shape.
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "private_key",
        "session_key",
        "db_path",
        "database_path",
        "config_path",
        "config_dir",
        "path",
        "static_api_key",
        "oauth_client_secret",
    }
)

_TRACEBACK_HEADER = "Traceback (most recent call last):"

# Order matters. Auth-scheme headers are handled before generic key=value so
# the scheme keyword ("Bearer") survives for operators while the credential
# itself never does.
_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # 1. Whole traceback block -> single marker.
    (
        re.compile(re.escape(_TRACEBACK_HEADER) + r".*\Z", re.DOTALL),
        f"{REDACTED} (traceback suppressed)",
    ),
    # 2. Stray traceback frames outside a full block.
    (
        re.compile(r'(?m)^\s*File "[^"]*", line \d+.*$'),
        f"  {REDACTED} (frame suppressed)",
    ),
    # 3. Authorization scheme + credential.
    (
        re.compile(r"(?i)\b(Bearer|Basic|Digest|DPoP)\s+[A-Za-z0-9._~+/=-]{6,}"),
        lambda m: f"{m.group(1)} {REDACTED}",  # type: ignore[arg-type]
    ),
    # 4. JSON Web Tokens anywhere.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
        REDACTED,
    ),
    # 5. Credential-bearing URLs (host is kept so the log stays useful).
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://[^\s/:@]+:[^\s/@]+@"),
        lambda m: f"{m.group(1)}://{REDACTED}@",  # type: ignore[arg-type]
    ),
    # 6. Well-known bare token prefixes.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), REDACTED),
    # 7. key=value / key: value secrets ("authorization" is rule 3's job).
    #
    # Finding H-2 (Phase 10 task 10.6 security review): the keyword used to be
    # anchored with a leading ``\b``, so environment-variable style names whose
    # *suffix* is the keyword -- ``RECALL_MCP_TOKEN_SECRET=...``,
    # ``RECALL_MCP_DIGEST_KEY=...`` -- matched nothing and the raw value
    # survived into piped subprocess stderr (``servicectl``).  The optional
    # ``(?:[A-Za-z0-9]+[_-])*`` prefix accepts those namespaced names while
    # still requiring a ``_``/``-`` separator immediately before the keyword,
    # which is what keeps ordinary words ("monkey=1", "spoken=yes") out.
    (
        re.compile(
            r"(?i)\b((?:[A-Za-z0-9]+[_-])*"
            r"(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token"
            r"|client[_-]?secret|secret|token|password|passwd|pwd|private[_-]?key"
            r"|session[_-]?key|key|cookie))\b\s*[\"']?\s*[:=]\s*[\"']?([^\s\"',}&]+)"
        ),
        lambda m: m.group(0).replace(m.group(2), REDACTED),  # type: ignore[arg-type]
    ),
    # 8. UNC shares before drive letters.
    (re.compile(r"\\\\[^\s\"'<>|]+"), REDACTED),
    # 9. Windows absolute paths. The lookbehind keeps `https://` intact.
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|)\]]*"), REDACTED),
    # 10. POSIX home/system paths.
    (
        re.compile(
            r"(?<![\w.])/(?:home|Users|users|root|private|mnt|var|tmp|etc|opt|usr|proc)"
            r"(?:/[^\s\"'<>|,)\]]*)*"
        ),
        REDACTED,
    ),
)


def redact_text(value: Any) -> str:
    """Return ``value`` as text with every known secret shape removed."""

    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_path(value: Any) -> str:
    """Paths are never wire- or log-safe: always collapse them."""

    if value is None:
        return ""
    return REDACTED


def redact_exception(exc: BaseException) -> str:
    """Class name plus a redacted message. Never a traceback."""

    return f"{type(exc).__name__}: {redact_text(str(exc))}"


def redact_mapping(value: Any) -> Any:
    """Recursively redact a JSON-ish structure without mutating the input."""

    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in SENSITIVE_KEYS:
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact_mapping(item)
        return cleaned
    if isinstance(value, (str, bytes)):
        return redact_text(value.decode("utf-8", "replace") if isinstance(value, bytes) else value)
    if isinstance(value, Sequence):
        return [redact_mapping(item) for item in value]
    return value


__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "redact_exception",
    "redact_mapping",
    "redact_path",
    "redact_text",
]
