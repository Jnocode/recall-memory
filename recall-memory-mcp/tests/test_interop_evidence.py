"""Fail-closed contract for Phase 7/8 real-host interop evidence.

These tests do not connect a host and cannot make Gate 7 or Gate 8 pass. They
only prevent a future operator from turning placeholders, unbound hashes, or a
single local call into a false external-E2E claim.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import recall_memory_mcp.interop_evidence as interop_evidence
from recall_memory_mcp.interop_evidence import main, validate_matrix_document


AUTHORITY_ENDPOINT = "https://recall.invalid/mcp"
AUTHORITY_HASH = hashlib.sha256(AUTHORITY_ENDPOINT.encode("utf-8")).hexdigest()
SESSION_FINGERPRINT = "b" * 64
CAPTURED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
MVP_TOOLS = [
    "memory_search",
    "memory_get",
    "memory_recent",
    "memory_add",
    "memory_replace",
    "memory_remove",
    "memory_status",
]


def _artifact(root: Path, artifact_id: str, kind: str, body: dict[str, object]) -> dict[str, str]:
    path = root / f"{artifact_id}.json"
    payload = {
        "schema_version": "1.0.0",
        "kind": kind,
        "captured_at": CAPTURED_AT,
        **body,
    }
    text = json.dumps(payload, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "captured_at": CAPTURED_AT,
    }


def _not_run_client(client_id: str = "kiro") -> dict[str, object]:
    return {
        "client_id": client_id,
        "client_class": "ide_host",
        "status": "not_run",
        "client_version": None,
        "authority_endpoint_identity_sha256": None,
        "transport": None,
        "auth_registration_path": None,
        "oauth_metadata_discovered": None,
        "scope_mapping": {"required_exact_scope": "project:recall", "observed": None},
        "tools_discovered": [],
        "tools_list_artifact_ids": [],
        "session_lifecycle": {
            "post": None,
            "get": None,
            "delete": None,
            "session_id_observed": None,
            "session_fingerprint_sha256": None,
            "artifact_ids": [],
        },
        "calls": [],
        "external_read_back": {
            "status": "not_run",
            "reader_client_id": None,
            "memory_id": None,
            "revision": None,
            "content_sha256": None,
            "authority_endpoint_identity_sha256": None,
            "artifact_ids": [],
        },
        "evidence": {"captured_at": None, "artifacts": []},
    }


def _matrix(client: dict[str, object] | None = None) -> dict[str, object]:
    client = client or _not_run_client()
    return {
        "schema_version": "1.2.0",
        "generated_at": CAPTURED_AT,
        "authority": {
            "endpoint": None,
            "endpoint_identity_sha256": None,
            "deployment_mode": "not_configured",
            "server_version": None,
            "control_plane_poll_observed": None,
            "workspace_association_observed": None,
            "evidence": {"captured_at": None, "artifacts": []},
        },
        "evidence_contract": {
            "allowed_status": ["not_run", "blocked", "failed", "passed"],
            "unknown_values_must_be_null": True,
            "secrets_forbidden": True,
            "required_client_ids": [client["client_id"]],
        },
        "clients": [client],
    }


def _passed_matrix(root: Path, client_id: str = "kiro", prefix: str = "") -> dict[str, object]:
    def aid(name: str) -> str:
        return f"{prefix}{name}"

    client = _not_run_client(client_id)
    artifacts = [
        _artifact(
            root,
            aid("identity"),
            "client_identity",
            {"client_id": client_id, "client_class": "ide_host", "client_version": "1.0.337"},
        ),
        _artifact(
            root,
            aid("tools"),
            "tools_list_trace",
            {
                "client_id": client_id,
                "client_version": "1.0.337",
                "authority_endpoint_identity_sha256": AUTHORITY_HASH,
                "transport": "streamable_http",
                "auth_registration_path": "dcr",
                "oauth_metadata_discovered": True,
                "tools": MVP_TOOLS,
            },
        ),
        *[
            _artifact(
                root,
                aid(name),
                f"session_{name}_trace",
                {
                    "client_id": client_id,
                    "authority_endpoint_identity_sha256": AUTHORITY_HASH,
                    "method": name.upper(),
                    "success": True,
                    "session_fingerprint_sha256": SESSION_FINGERPRINT,
                },
            )
            for name in ("post", "get", "delete")
        ],
        _artifact(
            root,
            aid("call"),
            "tool_call_trace",
            {
                "client_id": client_id,
                "authority_endpoint_identity_sha256": AUTHORITY_HASH,
                "session_fingerprint_sha256": SESSION_FINGERPRINT,
                "tool": "memory_status",
                "outcome": "passed",
                "request_id": "req-status-0001",
                "scope": "project:recall",
                "memory_id": None,
                "revision": None,
                "content_sha256": None,
            },
        ),
    ]
    client.update(
        {
            "status": "passed",
            "client_version": "1.0.337",
            "authority_endpoint_identity_sha256": AUTHORITY_HASH,
            "transport": "streamable_http",
            "auth_registration_path": "dcr",
            "oauth_metadata_discovered": True,
            "scope_mapping": {"required_exact_scope": "project:recall", "observed": "project:recall"},
            "tools_discovered": list(MVP_TOOLS),
            "tools_list_artifact_ids": [aid("tools")],
            "session_lifecycle": {
                "post": True,
                "get": True,
                "delete": True,
                "session_id_observed": True,
                "session_fingerprint_sha256": SESSION_FINGERPRINT,
                "artifact_ids": [aid("post"), aid("get"), aid("delete")],
            },
            "calls": [
                {
                    "tool": "memory_status",
                    "outcome": "passed",
                    "captured_at": CAPTURED_AT,
                    "authority_endpoint_identity_sha256": AUTHORITY_HASH,
                    "session_fingerprint_sha256": SESSION_FINGERPRINT,
                    "artifact_ids": [aid("call")],
                }
            ],
            "evidence": {
                "captured_at": CAPTURED_AT,
                "artifacts": artifacts,
            },
        }
    )
    matrix = _matrix(client)
    matrix["authority"] = {
        "endpoint": AUTHORITY_ENDPOINT,
        "endpoint_identity_sha256": AUTHORITY_HASH,
        "deployment_mode": "remote",
        "server_version": "0.1.0",
        "control_plane_poll_observed": None,
        "workspace_association_observed": None,
        "evidence": {"captured_at": None, "artifacts": []},
    }
    return matrix


def _errors(document: dict[str, object], root: Path) -> tuple[str, ...]:
    return validate_matrix_document(document, artifact_root=root).errors


def _rewrite_artifact(document, root: Path, artifact_id: str, client_index: int = 0, **changes) -> None:
    entries = document["clients"][client_index]["evidence"]["artifacts"]
    entry = next(item for item in entries if item["artifact_id"] == artifact_id)
    path = root / entry["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "captured_at" in changes:
        entry["captured_at"] = changes["captured_at"]


def _valid_external_readback_matrix(root: Path) -> dict[str, object]:
    document = _passed_matrix(root, "kiro", "writer-")
    reader_document = _passed_matrix(root, "claude_desktop", "reader-")
    reader = reader_document["clients"][0]
    document["clients"].append(reader)
    document["evidence_contract"]["required_client_ids"] = ["kiro", "claude_desktop"]

    memory_id = "mem-canary"
    content_sha256 = "d" * 64
    writer = document["clients"][0]
    writer["calls"][0]["tool"] = "memory_add"
    _rewrite_artifact(
        document,
        root,
        "writer-call",
        tool="memory_add",
        memory_id=memory_id,
        revision=1,
        content_sha256=content_sha256,
    )
    reader["calls"][0]["tool"] = "memory_get"
    _rewrite_artifact(
        document,
        root,
        "reader-call",
        client_index=1,
        tool="memory_get",
        memory_id=memory_id,
        revision=1,
        content_sha256=content_sha256,
    )
    readback = _artifact(
        root,
        "external-readback",
        "external_read_back_trace",
        {
            "writer_client_id": "kiro",
            "reader_client_id": "claude_desktop",
            "authority_endpoint_identity_sha256": AUTHORITY_HASH,
            "writer_call_artifact_id": "writer-call",
            "reader_call_artifact_id": "reader-call",
            "scope": "project:recall",
            "memory_id": memory_id,
            "revision": 1,
            "content_sha256": content_sha256,
            "outcome": "passed",
        },
    )
    writer["evidence"]["artifacts"].append(readback)
    writer["external_read_back"] = {
        "status": "passed",
        "reader_client_id": "claude_desktop",
        "memory_id": memory_id,
        "revision": 1,
        "content_sha256": content_sha256,
        "authority_endpoint_identity_sha256": AUTHORITY_HASH,
        "artifact_ids": ["external-readback"],
    }
    return document


def test_current_not_run_shape_is_valid_and_makes_no_pass_claim(tmp_path):
    report = validate_matrix_document(_matrix(), artifact_root=tmp_path)
    assert report.ok is True
    assert report.passed_client_ids == ()
    assert report.external_read_back_client_ids == ()


def test_complete_real_host_shape_is_structurally_valid(tmp_path):
    report = validate_matrix_document(_passed_matrix(tmp_path), artifact_root=tmp_path)
    assert report.ok is True, report.errors
    assert report.passed_client_ids == ("kiro",)


@pytest.mark.parametrize(
    ("field", "value", "error_fragment"),
    [
        ("deployment_mode", "invented", "deployment_mode"),
        ("endpoint", None, "configured authority requires an endpoint"),
        ("server_version", None, "configured authority requires an observed server version"),
        ("endpoint", "http://recall.invalid/mcp", "requires an HTTPS endpoint"),
        ("endpoint", "https://recall.invalid/mcp?token=unsafe", "must not contain query"),
        ("endpoint", "https://recall.invalid/mcp?", "must not contain query"),
        ("endpoint", "https://recall.invalid/mcp#", "must not contain a fragment"),
        ("endpoint", "https://recall.invalid/mcp\n", "control characters"),
    ],
)
def test_configured_authority_requires_known_mode_and_safe_observed_endpoint(
    tmp_path, field, value, error_fragment
):
    document = _passed_matrix(tmp_path)
    document["authority"][field] = value

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert any(error_fragment in error for error in report.errors)
    assert report.ok is False
    assert report.passed_client_ids == ()


def test_local_authority_allows_only_loopback_mcp_endpoint(tmp_path):
    document = _matrix()
    local_endpoint = "http://localhost:8765/mcp"
    document["authority"].update(
        {
            "deployment_mode": "local",
            "endpoint": local_endpoint,
            "endpoint_identity_sha256": hashlib.sha256(local_endpoint.encode("utf-8")).hexdigest(),
            "server_version": "0.1.0",
        }
    )
    assert validate_matrix_document(document, artifact_root=tmp_path).ok is True

    non_loopback_endpoint = "http://192.0.2.10:8765/mcp"
    document["authority"]["endpoint"] = non_loopback_endpoint
    document["authority"]["endpoint_identity_sha256"] = hashlib.sha256(
        non_loopback_endpoint.encode("utf-8")
    ).hexdigest()
    errors = _errors(document, tmp_path)
    assert any("loopback" in error for error in errors)


def _rewrite_authority_artifact(document, root: Path, artifact_id: str, **changes) -> None:
    entries = document["authority"]["evidence"]["artifacts"]
    entry = next(item for item in entries if item["artifact_id"] == artifact_id)
    path = root / entry["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "captured_at" in changes:
        entry["captured_at"] = changes["captured_at"]


def _attach_tunnel_control_plane_evidence(document, root: Path) -> None:
    trace = _artifact(
        root,
        "tunnel-control-plane",
        "tunnel_control_plane_trace",
        {
            "authority_endpoint_identity_sha256": AUTHORITY_HASH,
            "deployment_mode": "tunnel_dev",
            "tunnel_client_version": "0.0.12",
            "control_plane_poll_ok": True,
            "workspace_association_observed": True,
            "result": "passed",
        },
    )
    document["authority"].update(
        {
            "control_plane_poll_observed": True,
            "workspace_association_observed": True,
            "evidence": {"captured_at": CAPTURED_AT, "artifacts": [trace]},
        }
    )


def test_tunnel_dev_requires_control_plane_and_workspace_association_evidence(tmp_path):
    document = _passed_matrix(tmp_path)
    document["authority"]["deployment_mode"] = "tunnel_dev"

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert report.ok is False
    assert report.passed_client_ids == ()
    assert any("control-plane poll" in error for error in report.errors)
    assert any("workspace association" in error for error in report.errors)


def test_tunnel_dev_accepts_bound_typed_control_plane_evidence(tmp_path):
    document = _passed_matrix(tmp_path)
    document["authority"]["deployment_mode"] = "tunnel_dev"
    _attach_tunnel_control_plane_evidence(document, tmp_path)

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert report.ok is True, report.errors
    assert report.passed_client_ids == ("kiro",)


def test_endpoint_identity_hash_cannot_be_replayed_for_another_endpoint(tmp_path):
    document = _passed_matrix(tmp_path)
    document["authority"]["deployment_mode"] = "tunnel_dev"
    _attach_tunnel_control_plane_evidence(document, tmp_path)
    document["authority"]["endpoint"] = "https://unrelated-authority.invalid/mcp"

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert report.ok is False
    assert report.passed_client_ids == ()
    assert any("endpoint identity SHA-256 does not match endpoint" in error for error in report.errors)


@pytest.mark.parametrize(
    ("change", "error_fragment"),
    [
        ({"control_plane_poll_ok": False}, "control-plane poll"),
        ({"workspace_association_observed": False}, "workspace association"),
        ({"authority_endpoint_identity_sha256": "e" * 64}, "authority identity"),
        ({"result": "failed"}, "result"),
    ],
)
def test_tunnel_dev_rejects_unproven_or_unbound_control_plane_trace(
    tmp_path, change, error_fragment
):
    document = _passed_matrix(tmp_path)
    document["authority"]["deployment_mode"] = "tunnel_dev"
    _attach_tunnel_control_plane_evidence(document, tmp_path)
    _rewrite_authority_artifact(document, tmp_path, "tunnel-control-plane", **change)

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert report.ok is False
    assert report.passed_client_ids == ()
    assert any(error_fragment in error for error in report.errors)


def test_naive_freshness_reference_time_fails_closed_without_raising(tmp_path):
    report = validate_matrix_document(
        _passed_matrix(tmp_path),
        artifact_root=tmp_path,
        evidence_reference_time=datetime.now(),
        max_evidence_age=timedelta(hours=24),
    )

    assert report.ok is False
    assert report.passed_client_ids == ()
    assert any("evidence_reference_time must be offset-aware" in error for error in report.errors)


@pytest.mark.parametrize("placeholder", [None, "", "unknown", "N/A", "latest", "TBD"])
def test_pass_rejects_missing_or_placeholder_client_version(tmp_path, placeholder):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["client_version"] = placeholder
    assert any("client_version" in error for error in _errors(document, tmp_path))


def test_pass_requires_all_mvp_tools_and_tools_list_trace(tmp_path):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["tools_discovered"].remove("memory_remove")
    document["clients"][0]["tools_list_artifact_ids"] = []
    errors = _errors(document, tmp_path)
    assert any("MVP tools" in error for error in errors)
    assert any("tools_list" in error for error in errors)


def test_pass_requires_real_call_not_only_discovery(tmp_path):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["calls"] = []
    assert any("successful tool call" in error for error in _errors(document, tmp_path))


@pytest.mark.parametrize("invalid_tool", ["invented_tool", "", None, ["memory_status"]])
def test_pass_rejects_successful_call_to_non_mvp_tool(tmp_path, invalid_tool):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["calls"][0]["tool"] = invalid_tool
    _rewrite_artifact(document, tmp_path, "call", tool=invalid_tool)

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert any("MVP tool" in error for error in report.errors)
    assert report.ok is False
    assert report.passed_client_ids == ()


def test_pass_requires_stateful_post_get_delete_and_same_session(tmp_path):
    document = _passed_matrix(tmp_path)
    lifecycle = document["clients"][0]["session_lifecycle"]
    lifecycle["get"] = False
    lifecycle["session_fingerprint_sha256"] = "c" * 64
    errors = _errors(document, tmp_path)
    assert any("GET" in error for error in errors)
    assert any("session fingerprint" in error for error in errors)


def test_pass_requires_method_specific_session_artifacts(tmp_path):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["session_lifecycle"]["artifact_ids"] = ["post", "get"]
    errors = _errors(document, tmp_path)
    assert any("session_delete_trace" in error for error in errors)


def test_declared_artifact_kind_must_match_typed_payload_kind(tmp_path):
    document = _passed_matrix(tmp_path)
    _rewrite_artifact(document, tmp_path, "post", kind="tool_call_trace")
    assert any("payload kind" in error for error in _errors(document, tmp_path))


def test_session_trace_payload_is_bound_to_authority_client_and_session(tmp_path):
    document = _passed_matrix(tmp_path)
    _rewrite_artifact(
        document,
        tmp_path,
        "post",
        authority_endpoint_identity_sha256="e" * 64,
        client_id="not-kiro",
        session_fingerprint_sha256="f" * 64,
    )
    errors = _errors(document, tmp_path)
    assert any("session trace authority" in error for error in errors)
    assert any("session trace client" in error for error in errors)
    assert any("session trace fingerprint" in error for error in errors)


def test_tools_trace_payload_is_bound_to_matrix_claims(tmp_path):
    document = _passed_matrix(tmp_path)
    _rewrite_artifact(document, tmp_path, "tools", tools=["memory_status"], transport="stdio")
    errors = _errors(document, tmp_path)
    assert any("tools trace tools" in error for error in errors)
    assert any("tools trace transport" in error for error in errors)


def test_pass_requires_exact_observed_scope_and_call_trace_scope(tmp_path):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["scope_mapping"]["observed"] = "global"
    _rewrite_artifact(document, tmp_path, "call", scope="global")
    assert any("scope_mapping" in error for error in _errors(document, tmp_path))

    document = _passed_matrix(tmp_path)
    _rewrite_artifact(document, tmp_path, "call", scope="global")
    assert any("tool call trace scope" in error for error in _errors(document, tmp_path))


def test_artifact_hash_is_recomputed_from_disk(tmp_path):
    document = _passed_matrix(tmp_path)
    (tmp_path / "call.json").write_text('{"tampered": true}\n', encoding="utf-8")
    report = validate_matrix_document(document, artifact_root=tmp_path)
    assert any("sha256 mismatch" in error for error in report.errors)
    assert report.passed_client_ids == ()


def test_missing_artifact_fails_closed(tmp_path):
    document = _passed_matrix(tmp_path)
    (tmp_path / "tools.json").unlink()
    assert any("does not exist" in error for error in _errors(document, tmp_path))


@pytest.mark.parametrize("unsafe", ["../escape.json", "C:/Users/Jun/trace.json", "/tmp/trace.json", "file:///tmp/x"])
def test_artifact_path_must_be_relative_and_contained(tmp_path, unsafe):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["evidence"]["artifacts"][0]["path"] = unsafe
    assert any("artifact path" in error for error in _errors(document, tmp_path))


def test_symlink_escape_fails_closed(tmp_path):
    outside = tmp_path.parent / "outside-interop-trace.json"
    outside.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "escape.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted on this Windows host")
    document = _passed_matrix(tmp_path)
    entry = document["clients"][0]["evidence"]["artifacts"][0]
    entry["path"] = link.name
    entry["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    assert any("artifact path" in error for error in _errors(document, tmp_path))


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Authorization: Bearer abcdef123456",
        "Traceback (most recent call last):\n  File \"C:/Users/Jun/x.py\", line 1",
        "database at C:/Users/Jun/.recall/recall.db",
        "https://user:hunter2@example.invalid/mcp",
    ],
)
def test_secret_path_or_traceback_in_evidence_fails_closed(tmp_path, unsafe_text):
    document = _passed_matrix(tmp_path)
    path = tmp_path / "call.json"
    path.write_text(unsafe_text, encoding="utf-8")
    for entry in document["clients"][0]["evidence"]["artifacts"]:
        if entry["artifact_id"] == "call":
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert any("unsafe content" in error for error in _errors(document, tmp_path))


def test_unsafe_matrix_value_suppresses_all_pass_summaries(tmp_path):
    document = _passed_matrix(tmp_path)
    document["authority"]["endpoint"] = "https://user:hunter2@example.invalid/mcp"
    report = validate_matrix_document(document, artifact_root=tmp_path)
    assert any("unsafe secret" in error for error in report.errors)
    assert report.passed_client_ids == ()
    assert report.external_read_back_client_ids == ()


def test_unknown_booleans_are_null_not_false_for_not_run(tmp_path):
    document = _matrix()
    document["clients"][0]["oauth_metadata_discovered"] = False
    assert any("not_run" in error for error in _errors(document, tmp_path))


def test_not_run_cannot_carry_observation_or_artifact(tmp_path):
    document = _matrix()
    document["clients"][0]["client_version"] = "1.0.337"
    document["clients"][0]["calls"] = [{"tool": "memory_status"}]
    errors = _errors(document, tmp_path)
    assert any("not_run" in error for error in errors)


def test_duplicate_or_missing_required_clients_fail_closed(tmp_path):
    duplicate = _not_run_client()
    document = _matrix()
    document["clients"].append(duplicate)
    assert any("duplicate client_id" in error for error in _errors(document, tmp_path))

    document = _matrix()
    document["evidence_contract"]["required_client_ids"] = ["kiro", "chatgpt_desktop"]
    assert any("missing required clients" in error for error in _errors(document, tmp_path))


def test_external_read_back_binds_writer_and_reader_call_traces(tmp_path):
    document = _valid_external_readback_matrix(tmp_path)
    report = validate_matrix_document(document, artifact_root=tmp_path)
    assert report.ok is True, report.errors
    assert report.passed_client_ids == ("kiro", "claude_desktop")
    assert report.external_read_back_client_ids == ("kiro",)


@pytest.mark.parametrize("writer_status", ["blocked", "failed"])
def test_external_read_back_requires_writer_client_to_be_passed(tmp_path, writer_status):
    document = _valid_external_readback_matrix(tmp_path)
    document["clients"][0]["status"] = writer_status

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert any("writer client itself is not passed" in error for error in report.errors)
    assert report.ok is False
    assert report.external_read_back_client_ids == ()


def test_pass_binds_matrix_call_timestamp_to_typed_trace(tmp_path):
    document = _passed_matrix(tmp_path)
    document["clients"][0]["calls"][0]["captured_at"] = "2026-08-24T06:00:00+00:00"

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert any("captured_at does not match matrix" in error for error in report.errors)
    assert report.ok is False
    assert report.passed_client_ids == ()


def test_call_timestamp_binding_accepts_same_instant_with_different_offset(tmp_path):
    document = _passed_matrix(tmp_path)
    same_instant = datetime.fromisoformat(CAPTURED_AT).astimezone(timezone(timedelta(hours=8)))
    document["clients"][0]["calls"][0]["captured_at"] = same_instant.isoformat()

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert report.ok is True, report.errors
    assert report.passed_client_ids == ("kiro",)


def test_external_read_back_rejects_reader_call_before_writer_call(tmp_path):
    document = _valid_external_readback_matrix(tmp_path)
    writer, reader = document["clients"]
    writer["calls"][0]["captured_at"] = "2026-08-24T06:00:00+00:00"
    reader["calls"][0]["captured_at"] = "2026-08-24T04:00:00+00:00"
    _rewrite_artifact(
        document,
        tmp_path,
        "writer-call",
        captured_at="2026-08-24T06:00:00+00:00",
    )
    _rewrite_artifact(
        document,
        tmp_path,
        "reader-call",
        client_index=1,
        captured_at="2026-08-24T04:00:00+00:00",
    )
    _rewrite_artifact(
        document,
        tmp_path,
        "external-readback",
        captured_at="2026-08-24T07:00:00+00:00",
    )

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert any("reader call predates writer call" in error for error in report.errors)
    assert report.ok is False
    assert report.external_read_back_client_ids == ()


def test_external_read_back_trace_cannot_predate_reader_call(tmp_path):
    document = _valid_external_readback_matrix(tmp_path)
    _rewrite_artifact(
        document,
        tmp_path,
        "external-readback",
        captured_at="2026-08-24T04:00:00+00:00",
    )

    report = validate_matrix_document(document, artifact_root=tmp_path)

    assert any("read-back trace predates reader call" in error for error in report.errors)
    assert report.ok is False
    assert report.external_read_back_client_ids == ()


def test_external_read_back_payload_cannot_disagree_with_matrix(tmp_path):
    document = _valid_external_readback_matrix(tmp_path)
    _rewrite_artifact(document, tmp_path, "external-readback", content_sha256="e" * 64)
    assert any("read-back trace content" in error for error in _errors(document, tmp_path))


def test_external_read_back_rejects_mismatched_reader_call_result(tmp_path):
    document = _valid_external_readback_matrix(tmp_path)
    _rewrite_artifact(document, tmp_path, "reader-call", client_index=1, revision=2)
    assert any("reader call trace" in error for error in _errors(document, tmp_path))


def test_external_read_back_requires_a_different_real_client_and_bound_artifact(tmp_path):
    document = _passed_matrix(tmp_path)
    client = document["clients"][0]
    readback = _artifact(
        tmp_path,
        "readback",
        "external_read_back_trace",
        {"reader": "kiro", "memory_id": "mem-canary", "revision": 1},
    )
    client["evidence"]["artifacts"].append(readback)
    client["external_read_back"] = {
        "status": "passed",
        "reader_client_id": "kiro",
        "memory_id": "mem-canary",
        "revision": 1,
        "content_sha256": "d" * 64,
        "authority_endpoint_identity_sha256": AUTHORITY_HASH,
        "artifact_ids": ["readback"],
    }
    report = validate_matrix_document(document, artifact_root=tmp_path)
    assert any("different client" in error for error in report.errors)
    assert report.external_read_back_client_ids == ()


def test_external_read_back_authority_must_match(tmp_path):
    document = _passed_matrix(tmp_path)
    client = document["clients"][0]
    readback = _artifact(
        tmp_path,
        "readback",
        "external_read_back_trace",
        {"reader": "claude_desktop", "memory_id": "mem-canary", "revision": 1},
    )
    client["evidence"]["artifacts"].append(readback)
    client["external_read_back"] = {
        "status": "passed",
        "reader_client_id": "claude_desktop",
        "memory_id": "mem-canary",
        "revision": 1,
        "content_sha256": "d" * 64,
        "authority_endpoint_identity_sha256": "e" * 64,
        "artifact_ids": ["readback"],
    }
    assert any("authority" in error for error in _errors(document, tmp_path))


def test_schema_version_and_unknown_fields_fail_closed(tmp_path):
    document = _matrix()
    document["schema_version"] = "1.0.0"
    document["clients"][0]["invented_pass_flag"] = True
    errors = _errors(document, tmp_path)
    assert any("schema_version" in error for error in errors)
    assert any("unknown fields" in error for error in errors)


def test_noninteractive_cli_emits_machine_report_and_exit_code(tmp_path, capsys):
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_matrix()), encoding="utf-8")
    assert main([str(matrix), "--artifact-root", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": True,
        "errors": [],
        "passed_client_ids": [],
        "external_read_back_client_ids": [],
    }

    matrix.write_text("not json", encoding="utf-8")
    assert main([str(matrix), "--artifact-root", str(tmp_path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["passed_client_ids"] == []


def test_cli_gate_requirement_fails_closed_for_valid_not_run_matrix(tmp_path, capsys):
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_matrix()), encoding="utf-8")

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "kiro",
        ]
    ) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["requirements_met"] is False
    assert output["missing_passed_client_ids"] == ["kiro"]
    assert output["missing_external_read_back_client_ids"] == []


def test_cli_gate_requirement_accepts_only_observed_passed_client(tmp_path, capsys):
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_passed_matrix(tmp_path)), encoding="utf-8")

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "kiro",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["requirements_met"] is True
    assert output["missing_passed_client_ids"] == []

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "chatgpt_desktop",
        ]
    ) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["requirements_met"] is False
    assert output["missing_passed_client_ids"] == ["chatgpt_desktop"]


def test_cli_gate_requirement_rejects_evidence_older_than_24_hours(tmp_path, capsys, monkeypatch):
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_passed_matrix(tmp_path)), encoding="utf-8")
    captured_at = datetime.fromisoformat(CAPTURED_AT)
    monkeypatch.setattr(
        interop_evidence,
        "_utc_now",
        lambda: captured_at + timedelta(hours=24, seconds=1),
    )

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "kiro",
        ]
    ) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["requirements_met"] is False
    assert output["passed_client_ids"] == []
    assert any("older than the 24-hour gate window" in error for error in output["errors"])


def test_cli_gate_requirement_rejects_evidence_over_five_minutes_in_future(
    tmp_path, capsys, monkeypatch
):
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_passed_matrix(tmp_path)), encoding="utf-8")
    captured_at = datetime.fromisoformat(CAPTURED_AT)
    monkeypatch.setattr(
        interop_evidence,
        "_utc_now",
        lambda: captured_at - timedelta(minutes=5, seconds=1),
    )

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "kiro",
        ]
    ) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["passed_client_ids"] == []
    assert any("more than 5 minutes in the future" in error for error in output["errors"])


def test_cli_external_read_back_requirement_is_separate_from_client_pass(tmp_path, capsys):
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_valid_external_readback_matrix(tmp_path)), encoding="utf-8")

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "kiro",
            "--require-passed-client",
            "claude_desktop",
            "--require-external-read-back-client",
            "kiro",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["requirements_met"] is True
    assert output["missing_external_read_back_client_ids"] == []

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-external-read-back-client",
            "claude_desktop",
        ]
    ) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["requirements_met"] is False
    assert output["missing_passed_client_ids"] == []
    assert output["missing_external_read_back_client_ids"] == ["claude_desktop"]


def test_cli_gate_rejects_external_read_back_from_non_passed_writer(tmp_path, capsys):
    document = _valid_external_readback_matrix(tmp_path)
    document["clients"][0]["status"] = "blocked"
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(document), encoding="utf-8")

    assert main(
        [
            str(matrix),
            "--artifact-root",
            str(tmp_path),
            "--require-passed-client",
            "claude_desktop",
            "--require-external-read-back-client",
            "kiro",
        ]
    ) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["requirements_met"] is False
    assert output["external_read_back_client_ids"] == []
    assert output["missing_external_read_back_client_ids"] == ["kiro"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["matrix.json", "--require-passed-client"],
        ["matrix.json", "--unknown-flag"],
    ],
)
def test_cli_usage_error_is_machine_readable_and_distinct_from_unmet_gate(argv, capsys):
    assert main(argv) == 64
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False,
        "errors": ["invalid command-line arguments"],
        "passed_client_ids": [],
        "external_read_back_client_ids": [],
    }


@pytest.mark.parametrize(
    "unsafe_client_id",
    [
        "",
        "unknown",
        "C:/Users/Jun/private.txt",
        "/tmp/private.txt",
        "https://user:hunter2@example.invalid/mcp",
        "Authorization: Bearer unsafe-token-value-1234567890",
    ],
)
def test_cli_requirement_client_id_rejects_unsafe_values_without_echo(unsafe_client_id, capsys):
    assert main(["matrix.json", "--require-passed-client", unsafe_client_id]) == 64
    captured = capsys.readouterr()
    if unsafe_client_id:
        assert unsafe_client_id not in captured.out
        assert unsafe_client_id not in captured.err
    assert json.loads(captured.out)["errors"] == ["invalid command-line arguments"]


def test_cli_invalid_matrix_precedes_missing_gate_requirement(tmp_path, capsys):
    matrix = tmp_path / "matrix.json"
    matrix.write_text("not json", encoding="utf-8")

    assert main(
        [
            str(matrix),
            "--require-passed-client=kiro",
            "--require-external-read-back-client=kiro",
        ]
    ) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["requirements_met"] is False
    assert output["missing_passed_client_ids"] == ["kiro"]
    assert output["missing_external_read_back_client_ids"] == ["kiro"]
