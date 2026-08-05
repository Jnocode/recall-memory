"""Client half of the owner-scoped admin CLI (tasks 6.10 / 6.10a / 6.10b).

This module is the *only* way ``recall-memory-mcp memory ...`` reaches a
memory, and it can only reach one through the running authority:

* It never imports the repository, never opens SQLite and never accepts a
  ``--owner-id``.  If the authority is not running there is simply nothing to
  fall back to, and the command refuses (R8.10, design §9.1).
* The bearer token comes from the OS-bound local admin session file, which
  only the account that owns the authority can read.
* The endpoint is taken from that same session file **and re-validated as
  loopback** before a single byte is sent, so a tampered session file cannot
  turn the CLI into a token exfiltration tool.

Export destinations reuse the break-glass destination rules (no symlink, no
overwrite, no directory) and add one of their own: an export is *plaintext
memory content*, so it may not be written to another volume — an operator
who wants it off this machine must encrypt it with their own tooling first
(R8.6).  We refuse rather than pretend: there is no authenticated cipher in
the standard library, and shipping a home-made one would be worse than an
honest refusal.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from . import admin as admin_mod
from . import adminapi, breakglass, provisioning

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

OFFSITE_REFUSAL: Final[str] = (
    "the export destination is not on the same volume as the authority "
    "database. An export is plaintext memory content, so it must be written "
    "locally and encrypted with your own tooling before it leaves this "
    "machine (R8.6). Removable media, network shares and UNC paths are "
    "refused."
)


class AdminClientError(RuntimeError):
    """Base class for admin CLI client failures."""


class AdminClientRefused(AdminClientError):
    """The authority refused the request (403/401/409) or we refused to send it."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class AdminClientInvalid(AdminClientError):
    """The request was malformed (400)."""


class AdminClientUnavailable(AdminClientError):
    """The authority is not reachable or could not complete the request."""


class DestinationRefused(AdminClientError):
    """The export destination is unsafe."""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminClient:
    endpoint: str
    token: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    opener: Any = None

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.endpoint.rstrip('/')}{path}"
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Host": urlsplit(self.endpoint).netloc,
            },
        )
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc) from None
        except urllib.error.URLError as exc:
            raise AdminClientUnavailable(
                "the authority is not reachable; admin commands have no "
                "offline path - start `serve` first"
            ) from exc
        except OSError as exc:  # pragma: no cover - defensive
            raise AdminClientUnavailable("the authority is not reachable") from exc


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdminClientUnavailable("the authority returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise AdminClientUnavailable("the authority returned a malformed response")
    return payload


def _from_http_error(exc: urllib.error.HTTPError) -> AdminClientError:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        payload = {}
    detail = payload if isinstance(payload, dict) else {}
    status = exc.code
    if status in (401, 403):
        return AdminClientRefused(
            "not authorized: this session is not the owner-admin for that "
            "scope, or the grant no longer carries memory:admin",
            details=detail,
        )
    if status == 409:
        current = detail.get("current_revision")
        return AdminClientRefused(
            "revision conflict: the memory has moved on"
            + (f" (current revision: {current})" if current is not None else ""),
            details=detail,
        )
    if status == 400:
        return AdminClientInvalid("the authority rejected the request arguments")
    return AdminClientUnavailable(f"the authority could not complete the request ({status})")


def connect(settings: Any, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, opener: Any = None) -> AdminClient:
    """Load the OS-bound session and build a client for its endpoint."""

    try:
        session = adminapi.load_local_admin_session(settings.config_dir)
    except adminapi.AdminSessionError as exc:
        raise AdminClientUnavailable(str(exc)) from exc
    host = urlsplit(session.endpoint).hostname
    if host not in LOOPBACK_HOSTS:
        raise AdminClientRefused(
            "the local admin session points at a non-loopback endpoint; "
            "refusing to send an admin token off this machine"
        )
    return AdminClient(endpoint=session.endpoint, token=session.token, timeout=timeout, opener=opener)


# ---------------------------------------------------------------------------
# destination safety
# ---------------------------------------------------------------------------


def _volume_id(path: Path) -> Any:
    """Volume identity of an existing path (drive on Windows, device elsewhere)."""

    if os.name == "nt":
        return os.path.splitdrive(str(path))[0].lower()
    return os.stat(path).st_dev


def _is_unc(raw: str) -> bool:
    return raw.startswith("\\\\") or raw.startswith("//")


def prepare_export_destination(destination: str | Path, db_path: str | Path) -> Path:
    """Validate an export target before anything is read out of the authority."""

    raw = str(destination)
    if _is_unc(raw):
        raise DestinationRefused(OFFSITE_REFUSAL)
    target = Path(destination).expanduser()
    try:
        checked = breakglass._check_new_destination(target)
    except breakglass.DestinationRefusedError as exc:
        raise DestinationRefused(str(exc)) from exc
    parent = checked.parent if str(checked.parent) else Path(".")
    if not parent.is_dir():
        raise DestinationRefused("the destination directory does not exist")
    if parent.is_symlink():
        raise DestinationRefused("the destination directory is a symbolic link")
    try:
        same_volume = _volume_id(parent) == _volume_id(Path(db_path).parent)
    except OSError as exc:
        raise DestinationRefused("the destination volume could not be identified") from exc
    if not same_volume:
        raise DestinationRefused(OFFSITE_REFUSAL)
    return checked


def write_export_document(destination: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Write an export owner-only and read it back before reporting success."""

    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(destination, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    owner_only = provisioning.harden_file(destination)
    readback = json.loads(destination.read_text(encoding="utf-8"))
    if readback != document:
        raise AdminClientUnavailable("the export could not be read back byte-for-byte")
    return {
        "owner_only": owner_only,
        "memory_count": int(readback.get("memory_count", 0)),
        "tombstone_count": int(readback.get("tombstone_count", 0)),
        "includes_tombstones": bool(readback.get("includes_tombstones", False)),
    }


BACKUP_RETENTION_NOTICE: Final[str] = admin_mod.BACKUP_RETENTION_NOTICE


__all__ = [
    "BACKUP_RETENTION_NOTICE",
    "OFFSITE_REFUSAL",
    "AdminClient",
    "AdminClientError",
    "AdminClientInvalid",
    "AdminClientRefused",
    "AdminClientUnavailable",
    "DestinationRefused",
    "connect",
    "prepare_export_destination",
    "write_export_document",
]
