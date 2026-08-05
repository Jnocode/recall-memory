"""``memory export/restore/purge`` CLI tests (tasks 6.10 / 6.10a / 6.10b).

The CLI half is deliberately dumb: it proves *destination safety*, *refusal
behaviour* and *the absence of an offline path*.  The authority side of the
conversation is a recording fake opener, so these tests never need a live
server — but they do assert that the CLI would have refused to send anything
at all whenever it is not entitled to.

Every test runs ``cli.main`` with an explicit environment rooted at
``tmp_path``; nothing here can reach the operator's real configuration.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from recall_memory_mcp import adminapi, admincli, cli

SCOPE = "global"
MEMORY_ID = "mem-canary-0001"
ENDPOINT_PORT = 19884


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class RecordingOpener:
    """Stands in for ``urllib.request.urlopen``; records every attempt."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.requests: list[Any] = []
        self.responses = responses or {}

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        path = request.full_url.split(ENDPOINT_HOST, 1)[-1]
        payload = self.responses.get(path)
        if payload is None:
            raise AssertionError(f"unexpected admin call: {path}")
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)

    def paths(self) -> list[str]:
        return [request.full_url for request in self.requests]

    def body_of(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].data.decode("utf-8"))


ENDPOINT_HOST = f"127.0.0.1:{ENDPOINT_PORT}"


def make_env(tmp_path: Path) -> dict[str, str]:
    return {
        "RECALL_MCP_CONFIG_DIR": str(tmp_path / "authority"),
        "RECALL_MCP_MODE": "local",
        "RECALL_MCP_HOST": "127.0.0.1",
        "RECALL_MCP_PORT": str(ENDPOINT_PORT),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"),
        "TEMP": str(tmp_path / "temp"),
    }


def write_session(tmp_path: Path, *, endpoint: str | None = None) -> adminapi.LocalAdminSession:
    config_dir = tmp_path / "authority"
    config_dir.mkdir(parents=True, exist_ok=True)
    # a database file has to exist for the settings/volume checks
    db = config_dir / "authority.db"
    if not db.exists():
        db.write_bytes(b"")
    return adminapi.create_local_admin_session(
        config_dir, endpoint=endpoint or f"http://{ENDPOINT_HOST}"
    )


def run(
    argv: list[str], env: dict[str, str], *, opener: Any = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    if opener is not None:
        args.opener = opener
    code = args.handler(args, env, out, err)
    return code, out.getvalue(), err.getvalue()


EXPORT_RESPONSE = {
    "backup_retention_notice": admincli.BACKUP_RETENTION_NOTICE,
    "exported_at": "2026-08-05T00:00:00+00:00",
    "includes_tombstones": False,
    "memories": [
        {
            "content": "a durable decision",
            "content_hash": "sha256:abc",
            "created_at": "2026-08-05T00:00:00+00:00",
            "deleted_at": None,
            "kind": "note",
            "memory_id": MEMORY_ID,
            "revision": 1,
            "scope": SCOPE,
            "source_client": "local-operator",
            "source_conversation": None,
            "tags": ["canary"],
            "updated_at": "2026-08-05T00:00:00+00:00",
        }
    ],
    "memory_count": 1,
    "schema_version": 1,
    "scope": SCOPE,
    "tombstone_count": 0,
    "audit_event_id": "event-1",
    "auth": "local_admin_session",
}

PURGE_RESPONSE = {
    "audit_event_id": "event-2",
    "auth": "local_admin_session",
    "backup_retention_notice": admincli.BACKUP_RETENTION_NOTICE,
    "final_revision": 3,
    "memory_id": MEMORY_ID,
    "purged_at": "2026-08-05T00:00:00+00:00",
}

RESTORE_RESPONSE = {
    "audit_event_id": "event-3",
    "auth": "local_admin_session",
    "memory_id": MEMORY_ID,
    "restored_at": "2026-08-05T00:00:00+00:00",
    "revision": 3,
}


# ---------------------------------------------------------------------------
# there is no offline path
# ---------------------------------------------------------------------------


class TestNoOfflinePath:
    def test_export_without_a_running_authority_refuses(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        (tmp_path / "authority").mkdir(parents=True, exist_ok=True)
        code, out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(tmp_path / "out.json")], env
        )
        assert code == cli.EXIT_FAILURE
        assert "offline" in err
        assert out == ""
        assert not (tmp_path / "out.json").exists()

    def test_purge_without_a_running_authority_refuses(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        (tmp_path / "authority").mkdir(parents=True, exist_ok=True)
        code, _out, err = run(
            [
                "memory",
                "purge",
                MEMORY_ID,
                "--scope",
                SCOPE,
                "--expected-revision",
                "2",
                "--confirm",
                MEMORY_ID,
            ],
            env,
        )
        assert code == cli.EXIT_FAILURE
        assert "offline" in err

    def test_no_command_accepts_an_owner_id(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["memory", "export", "--scope", SCOPE, "--output", "x", "--owner-id", "someone"],
            ["memory", "purge", "m", "--scope", SCOPE, "--expected-revision", "1", "--owner-id", "x"],
            ["memory", "restore", "m", "--scope", SCOPE, "--expected-revision", "1", "--owner-id", "x"],
        ):
            with pytest.raises(SystemExit):
                parser.parse_args(argv)

    def test_memory_commands_have_no_db_path_override(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["memory", "export", "--scope", SCOPE, "--output", "x", "--db-path", "/tmp/x.db"]
            )

    @pytest.mark.parametrize(
        "argv",
        [
            ["db", "export", "x"],
            ["db", "purge", "m"],
            ["db", "memory-export", "x"],
            ["db", "grant-edit"],
            ["memory", "list"],
            ["memory", "search", "--scope", SCOPE],
        ],
    )
    def test_offline_allowlist_and_memory_allowlist_reject_extras(self, argv: list[str]) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# ---------------------------------------------------------------------------
# destination safety
# ---------------------------------------------------------------------------


class TestExportDestination:
    def test_refuses_to_overwrite_an_existing_file(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        destination = tmp_path / "already-there.json"
        destination.write_text("keep me", encoding="utf-8")
        opener = RecordingOpener()
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(destination)],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_REFUSED
        assert "already exists" in err
        assert opener.requests == [], "the export was requested before the target was checked"
        assert destination.read_text(encoding="utf-8") == "keep me"

    def test_refuses_a_directory(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        target = tmp_path / "a-directory"
        target.mkdir()
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(target)],
            env,
            opener=RecordingOpener(),
        )
        assert code == cli.EXIT_REFUSED
        assert "directory" in err

    def test_refuses_a_unc_destination(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", r"\\fileserver\share\export.json"],
            env,
            opener=RecordingOpener(),
        )
        assert code == cli.EXIT_REFUSED
        assert "encrypt" in err

    def test_refuses_another_volume(self, tmp_path: Path, monkeypatch: Any) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        calls: list[Path] = []

        def fake_volume(path: Path) -> str:
            calls.append(path)
            return "removable" if "usb" in str(path) else "local"

        monkeypatch.setattr(admincli, "_volume_id", fake_volume)
        removable = tmp_path / "usb"
        removable.mkdir()
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(removable / "export.json")],
            env,
            opener=RecordingOpener(),
        )
        assert code == cli.EXIT_REFUSED
        assert "same volume" in err
        assert calls, "the volume check never ran"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="POSIX symlink semantics")
    def test_refuses_a_symlinked_destination(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        real = tmp_path / "real.json"
        link = tmp_path / "link.json"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("this platform will not create a symlink for an unprivileged user")
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(link)],
            env,
            opener=RecordingOpener(),
        )
        assert code == cli.EXIT_REFUSED
        assert "symbolic link" in err

    def test_refuses_a_missing_destination_directory(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        code, _out, err = run(
            [
                "memory",
                "export",
                "--scope",
                SCOPE,
                "--output",
                str(tmp_path / "no" / "such" / "dir" / "export.json"),
            ],
            env,
            opener=RecordingOpener(),
        )
        assert code == cli.EXIT_REFUSED
        assert "directory does not exist" in err


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


class TestExportSuccess:
    def test_export_writes_owner_only_json_and_warns_about_retention(
        self, tmp_path: Path
    ) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        destination = tmp_path / "export.json"
        opener = RecordingOpener({"/admin/v1/memory/export": EXPORT_RESPONSE})
        code, out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(destination)],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_OK, err
        document = json.loads(destination.read_text(encoding="utf-8"))
        assert document["memory_count"] == 1
        assert document["includes_tombstones"] is False
        assert "auth" not in document, "transport metadata must not land in the export"
        assert "audit_event_id" not in document
        assert admincli.BACKUP_RETENTION_NOTICE in err
        assert "read-back OK" in out
        if hasattr(os, "getuid"):
            assert stat.S_IMODE(destination.stat().st_mode) & 0o077 == 0

    def test_export_sends_the_tombstone_flag(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        response = dict(EXPORT_RESPONSE, includes_tombstones=True, tombstone_count=1)
        opener = RecordingOpener({"/admin/v1/memory/export": response})
        code, out, _err = run(
            [
                "memory",
                "export",
                "--scope",
                SCOPE,
                "--output",
                str(tmp_path / "export.json"),
                "--include-tombstones",
            ],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_OK
        assert opener.body_of()["include_tombstones"] is True
        assert "includes tombstone: yes" in out

    def test_export_request_carries_the_session_token(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        session = write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/export": EXPORT_RESPONSE})
        run(
            ["memory", "export", "--scope", SCOPE, "--output", str(tmp_path / "e.json")],
            env,
            opener=opener,
        )
        header = opener.requests[0].get_header("Authorization")
        assert header == f"Bearer {session.token}"

    def test_export_never_prints_the_token(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        session = write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/export": EXPORT_RESPONSE})
        _code, out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(tmp_path / "e.json")],
            env,
            opener=opener,
        )
        assert session.token not in out
        assert session.token not in err


class TestPurgeCli:
    def test_purge_requires_an_exact_confirmation(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/purge": PURGE_RESPONSE})
        code, _out, err = run(
            ["memory", "purge", MEMORY_ID, "--scope", SCOPE, "--expected-revision", "2"],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_REFUSED
        assert f"--confirm {MEMORY_ID}" in err
        assert opener.requests == [], "an unconfirmed purge must never reach the authority"

    def test_purge_rejects_a_mismatched_confirmation(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/purge": PURGE_RESPONSE})
        code, _out, _err = run(
            [
                "memory",
                "purge",
                MEMORY_ID,
                "--scope",
                SCOPE,
                "--expected-revision",
                "2",
                "--confirm",
                "some-other-id",
            ],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_REFUSED
        assert opener.requests == []

    def test_purge_warns_about_backups_before_it_runs(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/purge": PURGE_RESPONSE})
        code, out, err = run(
            [
                "memory",
                "purge",
                MEMORY_ID,
                "--scope",
                SCOPE,
                "--expected-revision",
                "2",
                "--confirm",
                MEMORY_ID,
            ],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_OK, err
        assert "irreversible" in err
        assert "retention" in err.lower()
        assert "final revision  : 3" in out
        assert opener.body_of()["expected_revision"] == 2

    def test_purge_sends_an_idempotency_key(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/purge": PURGE_RESPONSE})
        run(
            [
                "memory",
                "purge",
                MEMORY_ID,
                "--scope",
                SCOPE,
                "--expected-revision",
                "2",
                "--confirm",
                MEMORY_ID,
            ],
            env,
            opener=opener,
        )
        key = opener.body_of()["idempotency_key"]
        assert key and len(key) >= 8


class TestRestoreCli:
    def test_restore_reports_the_new_revision(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path)
        opener = RecordingOpener({"/admin/v1/memory/restore": RESTORE_RESPONSE})
        code, out, _err = run(
            ["memory", "restore", MEMORY_ID, "--scope", SCOPE, "--expected-revision", "2"],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_OK
        assert "new revision    : 3" in out

    def test_refused_restore_explains_backup_retention(self, tmp_path: Path) -> None:
        import urllib.error

        env = make_env(tmp_path)
        write_session(tmp_path)
        error = urllib.error.HTTPError(
            url=f"http://{ENDPOINT_HOST}/admin/v1/memory/restore",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"error": "not_authorized"}).encode("utf-8")),
        )
        opener = RecordingOpener({"/admin/v1/memory/restore": error})
        code, _out, err = run(
            ["memory", "restore", MEMORY_ID, "--scope", SCOPE, "--expected-revision", "9"],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_REFUSED
        assert "unrecoverable from the authority" in err
        assert "your responsibility" in err

    def test_revision_conflict_is_reported_with_the_current_revision(
        self, tmp_path: Path
    ) -> None:
        import urllib.error

        env = make_env(tmp_path)
        write_session(tmp_path)
        error = urllib.error.HTTPError(
            url=f"http://{ENDPOINT_HOST}/admin/v1/memory/restore",
            code=409,
            msg="Conflict",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps({"error": "revision_conflict", "current_revision": 4}).encode("utf-8")
            ),
        )
        opener = RecordingOpener({"/admin/v1/memory/restore": error})
        code, _out, err = run(
            ["memory", "restore", MEMORY_ID, "--scope", SCOPE, "--expected-revision", "2"],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_REFUSED
        assert "current revision: 4" in err


# ---------------------------------------------------------------------------
# token handling
# ---------------------------------------------------------------------------


class TestSessionSafety:
    def test_a_non_loopback_session_endpoint_is_refused(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        write_session(tmp_path, endpoint="http://memories.example.com:443")
        opener = RecordingOpener()
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(tmp_path / "e.json")],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_REFUSED
        assert "non-loopback" in err
        assert opener.requests == [], "the token must never be sent off-machine"

    def test_an_expired_session_refuses_before_connecting(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        session = write_session(tmp_path)
        payload = json.loads(session.path.read_text(encoding="utf-8"))
        payload["expires_at"] = "2000-01-01T00:00:00+00:00"
        session.path.write_text(json.dumps(payload), encoding="utf-8")
        opener = RecordingOpener()
        code, _out, err = run(
            ["memory", "export", "--scope", SCOPE, "--output", str(tmp_path / "e.json")],
            env,
            opener=opener,
        )
        assert code == cli.EXIT_FAILURE
        assert "expired" in err
        assert opener.requests == []
