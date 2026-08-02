"""Task 1.18 — OS-backed authority DB ownership lock.

Requirements under test (requirements.md R1.5, design.md §11):

* A second authority process trying to own the same SQLite file **fails
  closed** with a typed error and never shares the file.
* The lock is an OS-backed exclusive advisory lock held for the process
  lifetime, so a crash (``kill -9`` / ``TerminateProcess``) releases it and a
  new process can safely take over.
* Lock metadata (instance id, start time, redacted DB identity) is
  *diagnostic only*: a stale metadata blob must never block a new owner, and
  deleting a PID file must never be the mutex.
* Paths whose filesystem cannot be locked reliably (network filesystems)
  fail closed in the MVP.

Every process-level test uses real ``subprocess`` interpreters, not threads,
because thread-based "processes" would not prove OS lock semantics.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recall.authority_lock import (  # noqa: E402
    AuthorityDatabaseLock,
    AuthorityLockError,
    AuthorityLockHeldError,
    AuthorityLockMetadata,
    AuthorityLockUnsupportedError,
    lock_path_for,
    probe_filesystem,
)

SRC_PATH = str(Path(__file__).resolve().parents[1] / "src")

# --------------------------------------------------------------------------
# Subprocess helpers: real interpreters, coordinated through stdin/stdout.
# --------------------------------------------------------------------------

_HOLDER_SCRIPT = """
import json, sys
sys.path.insert(0, {src!r})
from recall.authority_lock import AuthorityDatabaseLock

lock = AuthorityDatabaseLock({db!r})
metadata = lock.acquire()
print(json.dumps({{
    "status": "ACQUIRED",
    "instance_id": metadata.instance_id,
    "pid": metadata.pid,
    "db_identity": metadata.db_identity,
}}), flush=True)
# Hold the lock for the process lifetime until told to release (or killed).
line = sys.stdin.readline()
if line.strip() == "RELEASE":
    lock.release()
    print(json.dumps({{"status": "RELEASED"}}), flush=True)
else:
    print(json.dumps({{"status": "EXITING"}}), flush=True)
"""

_CONTENDER_SCRIPT = """
import json, sys
sys.path.insert(0, {src!r})
from recall.authority_lock import AuthorityDatabaseLock, AuthorityLockHeldError

try:
    metadata = AuthorityDatabaseLock({db!r}).acquire()
except AuthorityLockHeldError as exc:
    print(json.dumps({{
        "status": "REJECTED",
        "error": type(exc).__name__,
        "message": str(exc),
    }}))
else:
    print(json.dumps({{"status": "ACQUIRED", "instance_id": metadata.instance_id}}))
"""

_RACER_SCRIPT = """
import json, os, sys, time
sys.path.insert(0, {src!r})
from recall.authority_lock import AuthorityDatabaseLock, AuthorityLockHeldError

go = {go!r}
deadline = time.time() + 60
while not os.path.exists(go):
    if time.time() > deadline:
        print(json.dumps({{"status": "TIMEOUT"}}), flush=True)
        raise SystemExit(1)
    time.sleep(0.005)

lock = AuthorityDatabaseLock({db!r})
try:
    metadata = lock.acquire()
except AuthorityLockHeldError:
    print(json.dumps({{"status": "REJECTED"}}), flush=True)
else:
    print(json.dumps({{"status": "ACQUIRED", "instance_id": metadata.instance_id}}), flush=True)
    time.sleep(3.0)
    lock.release()
"""


def _start_holder(db_path: Path) -> tuple[subprocess.Popen, dict]:
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _HOLDER_SCRIPT.format(src=SRC_PATH, db=str(db_path))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    assert line, f"holder produced no output: {proc.stderr.read()}"
    payload = json.loads(line)
    assert payload["status"] == "ACQUIRED", payload
    return proc, payload


def _stop_holder(proc: subprocess.Popen, *, release: bool) -> None:
    if proc.poll() is None:
        try:
            proc.stdin.write("RELEASE\n" if release else "QUIT\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):  # pragma: no cover - process already gone
            pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - guard
        proc.kill()
        proc.wait(timeout=30)


def _contend(db_path: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _CONTENDER_SCRIPT.format(src=SRC_PATH, db=str(db_path))],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip())


def _authority_db(tmp_path: Path, name: str = "authority") -> Path:
    db_path = tmp_path / f"{name}.db"
    db_path.write_bytes(b"")  # the lock guards the file identity, not its schema
    return db_path


def _metadata_bytes(db_path: Path) -> bytes:
    """Read the sidecar's metadata region (offset 1+).

    Byte 0 is the reserved lock byte; on Windows it is a *mandatory* lock, so
    reading the whole file while an owner holds it raises PermissionError.
    Diagnostic readers are documented to start at offset 1.
    """

    with open(lock_path_for(db_path), "rb") as handle:
        handle.seek(1)
        return handle.read()


# --------------------------------------------------------------------------
# Second process fails closed
# --------------------------------------------------------------------------


def test_second_process_fails_closed_while_first_process_holds_the_lock(
    tmp_path: Path,
) -> None:
    db_path = _authority_db(tmp_path)
    holder, holder_payload = _start_holder(db_path)
    try:
        rejected = _contend(db_path)
        assert rejected["status"] == "REJECTED"
        assert rejected["error"] == "AuthorityLockHeldError"
        # Still rejected on a second attempt: no "eventually gives up" fallback.
        assert _contend(db_path)["status"] == "REJECTED"
    finally:
        _stop_holder(holder, release=True)

    # Once the holder released, a new process may own the authority.
    taken_over = _contend(db_path)
    assert taken_over["status"] == "ACQUIRED"
    assert taken_over["instance_id"] != holder_payload["instance_id"]


def test_rejection_message_leaks_no_path_or_metadata(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "leaky")
    holder, holder_payload = _start_holder(db_path)
    try:
        rejected = _contend(db_path)
    finally:
        _stop_holder(holder, release=True)

    message = rejected["message"]
    assert str(db_path) not in message
    assert db_path.name not in message
    assert str(tmp_path) not in message
    assert str(holder_payload["pid"]) not in message
    assert holder_payload["instance_id"] not in message


def test_only_one_of_many_racing_processes_wins(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "race")
    go_file = tmp_path / "go.flag"
    script = _RACER_SCRIPT.format(src=SRC_PATH, db=str(db_path), go=str(go_file))

    racers = [
        subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    try:
        time.sleep(1.5)  # let every interpreter reach the barrier
        go_file.write_text("go", encoding="utf-8")
        outputs = []
        for racer in racers:
            stdout, stderr = racer.communicate(timeout=120)
            assert racer.returncode == 0, stderr
            outputs.append(json.loads(stdout.strip().splitlines()[0]))
    finally:
        for racer in racers:
            if racer.poll() is None:  # pragma: no cover - guard
                racer.kill()

    acquired = [item for item in outputs if item["status"] == "ACQUIRED"]
    rejected = [item for item in outputs if item["status"] == "REJECTED"]
    assert len(acquired) == 1, outputs
    assert len(rejected) == 5, outputs


# --------------------------------------------------------------------------
# Crash recovery: the OS releases the lock, not a cleanup routine
# --------------------------------------------------------------------------


def test_os_releases_the_lock_when_the_holder_is_killed(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "crash")
    holder, holder_payload = _start_holder(db_path)

    assert _contend(db_path)["status"] == "REJECTED"

    # SIGKILL / TerminateProcess: no atexit handler, no finally block runs.
    holder.kill()
    holder.wait(timeout=30)
    holder.stdout.close()
    holder.stderr.close()
    holder.stdin.close()

    # The stale metadata of the dead owner is still on disk on purpose.
    stale = AuthorityDatabaseLock(db_path).read_metadata()
    assert stale is not None
    assert stale.instance_id == holder_payload["instance_id"]

    taken_over = _contend(db_path)
    assert taken_over["status"] == "ACQUIRED", taken_over
    assert taken_over["instance_id"] != holder_payload["instance_id"]


def test_lock_file_is_never_deleted_and_stale_metadata_never_blocks(
    tmp_path: Path,
) -> None:
    db_path = _authority_db(tmp_path, "stale")
    lock_file = lock_path_for(db_path)

    lock = AuthorityDatabaseLock(db_path)
    first = lock.acquire()
    assert lock_file.exists()
    lock.release()
    # A PID file that gets deleted is not a mutex: the sidecar must survive.
    assert lock_file.exists()
    assert lock.read_metadata().instance_id == first.instance_id

    # Forge metadata that claims a *live* process (this very test) owns it.
    forged = AuthorityLockMetadata(
        instance_id="forged-instance",
        acquired_at="2026-08-03T00:00:00+00:00",
        db_identity="sha256:0000000000000000",
        pid=os.getpid(),
    )
    AuthorityDatabaseLock(db_path)._write_metadata_for_test(forged)
    assert AuthorityDatabaseLock(db_path).read_metadata().instance_id == "forged-instance"

    # Nobody holds the OS lock, so the forged metadata must not stop takeover.
    second = AuthorityDatabaseLock(db_path)
    taken = second.acquire()
    try:
        assert taken.instance_id not in {"forged-instance", first.instance_id}
        assert second.read_metadata().instance_id == taken.instance_id
    finally:
        second.release()


def test_rejected_acquisition_does_not_rewrite_the_holder_metadata(
    tmp_path: Path,
) -> None:
    db_path = _authority_db(tmp_path, "nowrite")
    holder, holder_payload = _start_holder(db_path)
    try:
        before = _metadata_bytes(db_path)
        assert _contend(db_path)["status"] == "REJECTED"
        assert _metadata_bytes(db_path) == before
        current = AuthorityDatabaseLock(db_path).read_metadata()
        assert current.instance_id == holder_payload["instance_id"]
    finally:
        _stop_holder(holder, release=True)


# --------------------------------------------------------------------------
# In-process semantics
# --------------------------------------------------------------------------


def test_second_lock_object_in_the_same_process_fails_closed(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "same-process")
    first = AuthorityDatabaseLock(db_path)
    first.acquire()
    try:
        with pytest.raises(AuthorityLockHeldError):
            AuthorityDatabaseLock(db_path).acquire()
    finally:
        first.release()
    second = AuthorityDatabaseLock(db_path)
    second.acquire()
    second.release()


def test_double_acquire_on_the_same_object_is_refused(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "double")
    lock = AuthorityDatabaseLock(db_path)
    lock.acquire()
    try:
        with pytest.raises(AuthorityLockError):
            lock.acquire()
    finally:
        lock.release()


def test_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "ctx")
    with AuthorityDatabaseLock(db_path) as metadata:
        assert isinstance(metadata, AuthorityLockMetadata)
        assert _contend(db_path)["status"] == "REJECTED"
    assert _contend(db_path)["status"] == "ACQUIRED"


def test_release_without_acquire_is_a_noop(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "noop")
    lock = AuthorityDatabaseLock(db_path)
    lock.release()
    assert lock.read_metadata() is None
    assert lock.acquire().instance_id
    lock.release()


def test_two_different_databases_do_not_share_a_lock(tmp_path: Path) -> None:
    first_db = _authority_db(tmp_path, "alpha")
    second_db = _authority_db(tmp_path, "beta")
    assert lock_path_for(first_db) != lock_path_for(second_db)

    first = AuthorityDatabaseLock(first_db)
    second = AuthorityDatabaseLock(second_db)
    first.acquire()
    second.acquire()
    try:
        assert first.metadata.db_identity != second.metadata.db_identity
    finally:
        first.release()
        second.release()


def test_the_same_database_reached_by_a_different_path_shares_one_lock(
    tmp_path: Path,
) -> None:
    db_path = _authority_db(tmp_path, "canonical")
    indirect = tmp_path / "sub" / ".." / "canonical.db"
    (tmp_path / "sub").mkdir()

    holder = AuthorityDatabaseLock(db_path)
    holder.acquire()
    try:
        with pytest.raises(AuthorityLockHeldError):
            AuthorityDatabaseLock(indirect).acquire()
    finally:
        holder.release()


# --------------------------------------------------------------------------
# Metadata is diagnostic only and redacted
# --------------------------------------------------------------------------


def test_metadata_redacts_the_database_path(tmp_path: Path) -> None:
    db_path = _authority_db(tmp_path, "secret-name-do-not-leak")
    lock = AuthorityDatabaseLock(db_path)
    metadata = lock.acquire()
    try:
        assert metadata.db_identity.startswith("sha256:")
        assert "secret-name-do-not-leak" not in metadata.db_identity
        assert str(db_path) not in metadata.db_identity
        on_disk = _metadata_bytes(db_path).decode("utf-8", "replace")
        assert "secret-name-do-not-leak" not in on_disk
        assert str(tmp_path) not in on_disk
        assert metadata.pid == os.getpid()
        assert metadata.acquired_at.endswith("+00:00")
    finally:
        lock.release()


def test_unreadable_or_corrupt_metadata_is_reported_as_absent_not_trusted(
    tmp_path: Path,
) -> None:
    db_path = _authority_db(tmp_path, "corrupt")
    lock_file = lock_path_for(db_path)
    lock = AuthorityDatabaseLock(db_path)
    lock.acquire()
    lock.release()

    lock_file.write_bytes(b"\x00{not json at all")
    assert AuthorityDatabaseLock(db_path).read_metadata() is None
    # Corrupt metadata must not stop a legitimate owner from starting.
    fresh = AuthorityDatabaseLock(db_path)
    assert fresh.acquire().instance_id
    fresh.release()
    assert AuthorityDatabaseLock(db_path).read_metadata() is not None


# --------------------------------------------------------------------------
# Unreliable filesystems fail closed
# --------------------------------------------------------------------------


def test_unreliable_filesystem_fails_closed_and_creates_no_lock_file(
    tmp_path: Path,
) -> None:
    db_path = _authority_db(tmp_path, "network")
    lock_file = lock_path_for(db_path)

    lock = AuthorityDatabaseLock(
        db_path, filesystem_probe=lambda path: "simulated network filesystem"
    )
    with pytest.raises(AuthorityLockUnsupportedError) as excinfo:
        lock.acquire()

    assert "simulated network filesystem" in str(excinfo.value)
    assert str(db_path) not in str(excinfo.value)
    assert not lock_file.exists()
    assert lock.metadata is None


def test_local_temp_path_is_classified_reliable() -> None:
    assert probe_filesystem(Path(__file__).resolve()) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path classification")
def test_windows_unc_paths_are_classified_unreliable() -> None:
    reason = probe_filesystem(Path(r"\\file-server.invalid\share\recall.db"))
    assert reason is not None
    assert "unc" in reason.lower() or "network" in reason.lower()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux mount table")
def test_linux_network_fstypes_are_classified_unreliable() -> None:
    from recall.authority_lock import reason_for_fstype

    for fstype in ("nfs", "nfs4", "cifs", "smbfs", "9p", "fuse.sshfs"):
        assert reason_for_fstype(fstype) is not None
    assert reason_for_fstype("ext4") is None
    assert reason_for_fstype("xfs") is None
