"""OS-backed ownership lock for the shared Recall authority database.

Design contract (design.md §11, requirements.md R1.5):

* Exactly one authority process may own a given SQLite authority file.  A
  second process **fails closed** with :class:`AuthorityLockHeldError`; it
  never opens the file for writing "just in case".
* The mutex is an OS-backed exclusive advisory lock (``fcntl.flock`` on
  POSIX, ``msvcrt.locking`` on Windows) taken on a sidecar lock file and held
  for the process lifetime.  When the owner crashes the kernel drops the
  lock, so a new process can take over without any cleanup daemon.
* **A PID file is not a mutex.**  The sidecar file is never deleted on
  release, and its contents are never consulted when deciding whether the
  lock is available.  Metadata (instance id, acquisition time, redacted DB
  identity, pid) exists only so an operator can see *who* owns the DB; it is
  rewritten strictly *after* the OS lock has been won.
* Paths on filesystems where advisory locking is unreliable (network
  filesystems) fail closed in the MVP rather than silently allowing two
  owners.

Layout of the sidecar file::

    byte 0        reserved lock byte (never written after creation)
    byte 1..EOF   UTF-8 JSON metadata blob (diagnostic only)

Byte 0 is the only locked region, so a diagnostic reader can still read the
metadata from offset 1 while another process owns the authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable

if sys.platform == "win32":  # pragma: no cover - platform specific import
    import msvcrt
else:  # pragma: no cover - platform specific import
    import fcntl

__all__ = [
    "AuthorityDatabaseLock",
    "AuthorityLockError",
    "AuthorityLockHeldError",
    "AuthorityLockMetadata",
    "AuthorityLockUnsupportedError",
    "lock_path_for",
    "probe_filesystem",
    "reason_for_fstype",
]

LOCK_SUFFIX = ".authority-lock"
_METADATA_OFFSET = 1

# Filesystems whose advisory locks are either unimplemented, silently
# ignored, or only client-local.  Two authority processes on two hosts would
# both "win" the lock, which is precisely the failure we must never allow.
_UNRELIABLE_FSTYPES = frozenset(
    {
        "9p",
        "afs",
        "afpfs",
        "ceph",
        "cifs",
        "coda",
        "davfs",
        "davfs2",
        "fuse.davfs",
        "fuse.gcsfuse",
        "fuse.rclone",
        "fuse.s3fs",
        "fuse.sshfs",
        "fuseblk.ntfs",
        "gfs2",
        "glusterfs",
        "lustre",
        "ncpfs",
        "nfs",
        "nfs3",
        "nfs4",
        "ocfs2",
        "smb2",
        "smb3",
        "smbfs",
        "sshfs",
        "vboxsf",
    }
)


class AuthorityLockError(RuntimeError):
    """Base class for every authority ownership failure."""


class AuthorityLockHeldError(AuthorityLockError):
    """Another process (or another lock object) already owns the authority.

    The message is deliberately content-free: it never echoes the database
    path, the sidecar path, the holder's pid, or the holder's instance id,
    because those travel back to untrusted MCP clients in error envelopes.
    """

    def __init__(self) -> None:
        super().__init__("authority database is already owned by another process")


class AuthorityLockUnsupportedError(AuthorityLockError):
    """The path cannot be locked reliably, so ownership fails closed."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"authority database path cannot be locked reliably: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class AuthorityLockMetadata:
    """Diagnostic-only description of the current owner.

    Never a basis for takeover decisions: the OS lock is the only authority.
    """

    instance_id: str
    acquired_at: str
    db_identity: str
    pid: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolved(db_path: str | Path) -> Path:
    # ``strict=False`` so a not-yet-created authority file can still be locked
    # before the schema is migrated into it.
    return Path(db_path).expanduser().resolve(strict=False)


def lock_path_for(db_path: str | Path) -> Path:
    """Sidecar lock path for ``db_path`` (canonicalised, so aliases collide)."""

    resolved = _resolved(db_path)
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


def db_identity_for(db_path: str | Path) -> str:
    """Redacted, stable identity of the authority file.

    A truncated SHA-256 of the canonical path: enough for an operator to
    correlate two log lines, useless for learning where the DB lives.
    """

    digest = hashlib.sha256(str(_resolved(db_path)).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def reason_for_fstype(fstype: str) -> str | None:
    """Return a rejection reason for ``fstype``, or ``None`` if it is fine."""

    normalized = (fstype or "").strip().lower()
    if not normalized:
        return "filesystem type could not be determined"
    if normalized in _UNRELIABLE_FSTYPES:
        return f"filesystem type {normalized!r} does not provide reliable advisory locking"
    # ``fuse.<backend>`` covers arbitrary user-space network backends.
    if normalized.startswith("fuse.") and normalized not in {"fuse.ext4", "fuse.exfat"}:
        return f"filesystem type {normalized!r} does not provide reliable advisory locking"
    return None


def _probe_windows(path: Path) -> str | None:  # pragma: no cover - platform specific
    import ctypes

    raw = str(path)
    windows_path = PureWindowsPath(raw)
    drive = windows_path.drive
    if drive.startswith("\\\\") and not drive.upper().startswith("\\\\?\\"):
        return "UNC network path"
    if drive.upper().startswith("\\\\?\\UNC"):
        return "UNC network path"
    if not drive:
        return "path has no drive; filesystem type could not be determined"

    root = f"{drive}\\"
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    # 0 UNKNOWN, 1 NO_ROOT_DIR, 2 REMOVABLE, 3 FIXED, 4 REMOTE, 5 CDROM, 6 RAMDISK
    if drive_type == 4:
        return "mapped network drive"
    if drive_type in (0, 1):
        return "drive type could not be determined"
    if drive_type == 5:
        return "read-only optical media"
    return None


def _probe_linux(path: Path) -> str | None:  # pragma: no cover - platform specific
    try:
        raw = Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "mount table is unavailable; filesystem type could not be determined"

    best_mount = ""
    best_fstype = ""
    target = str(path)
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = fields[1].replace("\\040", " ")
        fstype = fields[2]
        if target == mount_point or target.startswith(
            mount_point if mount_point.endswith("/") else mount_point + "/"
        ):
            if len(mount_point) >= len(best_mount):
                best_mount = mount_point
                best_fstype = fstype
    if not best_mount:
        return "mount point could not be resolved"
    return reason_for_fstype(best_fstype)


def probe_filesystem(db_path: str | Path) -> str | None:
    """Return why ``db_path`` cannot be locked reliably, or ``None`` if it can.

    Unknown platforms fail closed: the MVP refuses to guess that a filesystem
    honours advisory locks.
    """

    # Probe the containing directory: the DB file itself may not exist yet.
    resolved = _resolved(db_path)
    probe_target = resolved.parent if resolved.parent != resolved else resolved

    if sys.platform == "win32":
        return _probe_windows(probe_target)
    if sys.platform.startswith("linux"):
        return _probe_linux(probe_target)
    return (
        f"filesystem type cannot be verified on platform {sys.platform!r}; "
        "refusing to assume reliable advisory locking"
    )


def _lock_exclusive_nonblocking(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - platform specific
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - platform specific
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - platform specific
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - platform specific
        fcntl.flock(fd, fcntl.LOCK_UN)


class AuthorityDatabaseLock:
    """Single-owner lock over one authority SQLite file."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        filesystem_probe: Callable[[Path], str | None] = probe_filesystem,
    ) -> None:
        self.db_path = _resolved(db_path)
        self.lock_path = lock_path_for(db_path)
        self._filesystem_probe = filesystem_probe
        self._fd: int | None = None
        self._metadata: AuthorityLockMetadata | None = None

    # -- state ----------------------------------------------------------

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    @property
    def metadata(self) -> AuthorityLockMetadata | None:
        """Metadata of the lock *this object* holds, or ``None``."""

        return self._metadata

    # -- acquisition ----------------------------------------------------

    def acquire(self) -> AuthorityLockMetadata:
        if self._fd is not None:
            raise AuthorityLockError("this instance already owns the authority database")

        reason = self._filesystem_probe(self.db_path)
        if reason is not None:
            # Fail closed *before* touching the filesystem: no sidecar file is
            # created on a path we refuse to serve from.
            raise AuthorityLockUnsupportedError(reason)

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_exclusive_nonblocking(fd)
        except OSError:
            os.close(fd)
            # Note: no PID inspection, no staleness heuristic, no takeover.
            raise AuthorityLockHeldError() from None
        except BaseException:
            os.close(fd)
            raise

        self._fd = fd
        metadata = AuthorityLockMetadata(
            instance_id=uuid.uuid4().hex,
            acquired_at=_utc_now(),
            db_identity=db_identity_for(self.db_path),
            pid=os.getpid(),
        )
        try:
            self._write_metadata(fd, metadata)
        except BaseException:
            self.release()
            raise
        self._metadata = metadata
        return metadata

    def release(self) -> None:
        """Release the OS lock.  The sidecar file is intentionally kept."""

        fd, self._fd = self._fd, None
        self._metadata = None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> AuthorityLockMetadata:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # -- diagnostics ----------------------------------------------------

    def read_metadata(self) -> AuthorityLockMetadata | None:
        """Read the sidecar metadata for diagnostics only.

        Returns ``None`` when the sidecar is missing, empty, truncated, or
        not parseable.  Callers must never use the result to decide whether
        they may own the database.
        """

        try:
            with open(self.lock_path, "rb") as handle:
                handle.seek(_METADATA_OFFSET)
                raw = handle.read()
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return AuthorityLockMetadata(
                instance_id=str(payload["instance_id"]),
                acquired_at=str(payload["acquired_at"]),
                db_identity=str(payload["db_identity"]),
                pid=int(payload["pid"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # -- internals ------------------------------------------------------

    @staticmethod
    def _write_metadata(fd: int, metadata: AuthorityLockMetadata) -> None:
        blob = json.dumps(asdict(metadata), sort_keys=True).encode("utf-8")
        os.ftruncate(fd, _METADATA_OFFSET)
        os.lseek(fd, _METADATA_OFFSET, os.SEEK_SET)
        os.write(fd, blob)
        os.fsync(fd)

    def _write_metadata_for_test(self, metadata: AuthorityLockMetadata) -> None:
        """Forge sidecar metadata without owning the lock (tests only).

        Production code must never call this: it exists so tests can prove
        that a plausible-looking stale owner record does not block takeover.
        """

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._write_metadata(fd, metadata)
        finally:
            os.close(fd)
