"""Fail-closed validation for real-host MCP interop evidence.

This module validates *evidence about* Phase 7/8.  It deliberately performs no
network calls and does not connect ChatGPT, Claude, Kiro, or any IDE.  A green
report means the matrix is internally consistent and every referenced artifact
exists, hashes correctly, and survives the project's leak scanner.  It is not a
Gate 7/8 result by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Mapping

from .redaction import redact_text

SCHEMA_VERSION: Final[str] = "1.1.0"
ALLOWED_STATUS: Final[frozenset[str]] = frozenset({"not_run", "blocked", "failed", "passed"})
MVP_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "memory_search",
        "memory_get",
        "memory_recent",
        "memory_add",
        "memory_replace",
        "memory_remove",
        "memory_status",
    }
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDERS = frozenset({"", "unknown", "n/a", "na", "latest", "tbd", "todo", "unverified"})

_ROOT_FIELDS = frozenset({"schema_version", "generated_at", "authority", "evidence_contract", "clients"})
_AUTHORITY_FIELDS = frozenset(
    {"endpoint", "endpoint_identity_sha256", "deployment_mode", "server_version"}
)
_CONTRACT_FIELDS = frozenset(
    {"allowed_status", "unknown_values_must_be_null", "secrets_forbidden", "required_client_ids"}
)
_CLIENT_FIELDS = frozenset(
    {
        "client_id",
        "client_class",
        "status",
        "client_version",
        "authority_endpoint_identity_sha256",
        "transport",
        "auth_registration_path",
        "oauth_metadata_discovered",
        "scope_mapping",
        "tools_discovered",
        "tools_list_artifact_ids",
        "session_lifecycle",
        "calls",
        "external_read_back",
        "evidence",
    }
)
_SCOPE_FIELDS = frozenset({"required_exact_scope", "observed"})
_SESSION_FIELDS = frozenset(
    {
        "post",
        "get",
        "delete",
        "session_id_observed",
        "session_fingerprint_sha256",
        "artifact_ids",
    }
)
_CALL_FIELDS = frozenset(
    {
        "tool",
        "outcome",
        "captured_at",
        "authority_endpoint_identity_sha256",
        "session_fingerprint_sha256",
        "artifact_ids",
    }
)
_READBACK_FIELDS = frozenset(
    {
        "status",
        "reader_client_id",
        "memory_id",
        "revision",
        "content_sha256",
        "authority_endpoint_identity_sha256",
        "artifact_ids",
    }
)
_EVIDENCE_FIELDS = frozenset({"captured_at", "artifacts"})
_ARTIFACT_FIELDS = frozenset({"artifact_id", "kind", "path", "sha256", "captured_at"})
_TRACE_COMMON_FIELDS = frozenset({"schema_version", "kind", "captured_at"})
_TRACE_FIELDS: Final[dict[str, frozenset[str]]] = {
    "client_identity": _TRACE_COMMON_FIELDS
    | frozenset({"client_id", "client_class", "client_version"}),
    "tools_list_trace": _TRACE_COMMON_FIELDS
    | frozenset(
        {
            "client_id",
            "client_version",
            "authority_endpoint_identity_sha256",
            "transport",
            "auth_registration_path",
            "oauth_metadata_discovered",
            "tools",
        }
    ),
    "session_post_trace": _TRACE_COMMON_FIELDS
    | frozenset(
        {
            "client_id",
            "authority_endpoint_identity_sha256",
            "method",
            "success",
            "session_fingerprint_sha256",
        }
    ),
    "session_get_trace": _TRACE_COMMON_FIELDS
    | frozenset(
        {
            "client_id",
            "authority_endpoint_identity_sha256",
            "method",
            "success",
            "session_fingerprint_sha256",
        }
    ),
    "session_delete_trace": _TRACE_COMMON_FIELDS
    | frozenset(
        {
            "client_id",
            "authority_endpoint_identity_sha256",
            "method",
            "success",
            "session_fingerprint_sha256",
        }
    ),
    "tool_call_trace": _TRACE_COMMON_FIELDS
    | frozenset(
        {
            "client_id",
            "authority_endpoint_identity_sha256",
            "session_fingerprint_sha256",
            "tool",
            "outcome",
            "request_id",
            "scope",
            "memory_id",
            "revision",
            "content_sha256",
        }
    ),
    "external_read_back_trace": _TRACE_COMMON_FIELDS
    | frozenset(
        {
            "writer_client_id",
            "reader_client_id",
            "authority_endpoint_identity_sha256",
            "writer_call_artifact_id",
            "reader_call_artifact_id",
            "scope",
            "memory_id",
            "revision",
            "content_sha256",
            "outcome",
        }
    ),
    "blocked_trace": _TRACE_COMMON_FIELDS | frozenset({"client_id", "code", "stage"}),
    "failure_trace": _TRACE_COMMON_FIELDS | frozenset({"client_id", "code", "stage"}),
}


@dataclass(frozen=True)
class MatrixValidationReport:
    """Machine-readable, immutable validation result."""

    errors: tuple[str, ...]
    passed_client_ids: tuple[str, ...]
    external_read_back_client_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], where: str, errors: list[str]) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if unknown:
        errors.append(f"{where}: unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{where}: missing fields: {', '.join(missing)}")


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _is_observed_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() not in _PLACEHOLDERS


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return value


def _scan_matrix_values(value: Any, where: str, errors: list[str]) -> None:
    """Reject known raw secret/path/traceback shapes in matrix string values."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            _scan_matrix_values(item, f"{where}.{key}", errors)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_matrix_values(item, f"{where}[{index}]", errors)
    elif isinstance(value, str) and redact_text(value) != value:
        errors.append(f"{where}: unsafe secret, absolute path, credential URL, or traceback content")


def _safe_artifact_path(root: Path, raw_path: Any) -> tuple[Path | None, str | None]:
    if not isinstance(raw_path, str) or not raw_path:
        return None, "artifact path must be a non-empty relative POSIX path"
    if "\\" in raw_path or ":" in raw_path or raw_path.startswith("/"):
        return None, "artifact path must be relative and must not be a drive, URI, or absolute path"
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None, "artifact path must be normalized and contained under artifact_root"

    root_resolved = root.resolve()
    candidate = root.joinpath(*pure.parts)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None, "artifact path escapes artifact_root (including through a symlink)"
    return resolved, None


def _validate_artifacts(
    evidence: Mapping[str, Any],
    *,
    client_label: str,
    artifact_root: Path | None,
    errors: list[str],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    _unknown_fields(evidence, _EVIDENCE_FIELDS, f"{client_label}.evidence", errors)
    artifacts_raw = _list(evidence.get("artifacts"))
    if artifacts_raw is None:
        errors.append(f"{client_label}.evidence.artifacts: must be a list")
        return {}, {}

    artifacts: dict[str, Mapping[str, Any]] = {}
    payloads: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(artifacts_raw):
        label = f"{client_label}.evidence.artifacts[{index}]"
        item = _mapping(raw)
        if item is None:
            errors.append(f"{label}: must be an object")
            continue
        _unknown_fields(item, _ARTIFACT_FIELDS, label, errors)
        artifact_id = item.get("artifact_id")
        if not _is_observed_text(artifact_id):
            errors.append(f"{label}.artifact_id: must be non-placeholder text")
            continue
        assert isinstance(artifact_id, str)
        if artifact_id in artifacts:
            errors.append(f"{client_label}: duplicate artifact_id {artifact_id!r}")
            continue
        artifacts[artifact_id] = item

        declared_kind = item.get("kind")
        if not _is_observed_text(declared_kind):
            errors.append(f"{label}.kind: must be non-placeholder text")
        if not _is_hash(item.get("sha256")):
            errors.append(f"{label}.sha256: must be 64 lowercase hex characters")
        if not _valid_timestamp(item.get("captured_at")):
            errors.append(f"{label}.captured_at: must be offset-aware RFC3339")

        if artifact_root is None:
            errors.append(f"{label}: artifact_root is required for file/hash read-back")
            continue
        path, path_error = _safe_artifact_path(artifact_root, item.get("path"))
        if path_error:
            errors.append(f"{label}: {path_error}")
            continue
        assert path is not None
        if not path.is_file():
            errors.append(f"{label}: artifact does not exist or is not a regular file")
            continue
        try:
            content = path.read_bytes()
        except OSError:
            errors.append(f"{label}: artifact could not be read")
            continue
        digest = hashlib.sha256(content).hexdigest()
        if digest != item.get("sha256"):
            errors.append(f"{label}: artifact sha256 mismatch")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{label}: artifact must be UTF-8 text for leak scanning")
            continue
        if redact_text(text) != text:
            errors.append(f"{label}: artifact contains unsafe content after leak scan")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            errors.append(f"{label}: artifact must be a typed JSON object")
            continue
        payload = _mapping(parsed)
        if payload is None:
            errors.append(f"{label}: artifact payload must be an object")
            continue
        payloads[artifact_id] = payload
        payload_kind = payload.get("kind")
        if payload_kind != declared_kind:
            errors.append(f"{label}: payload kind does not match declared artifact kind")
        expected_fields = _TRACE_FIELDS.get(str(payload_kind))
        if expected_fields is None:
            errors.append(f"{label}: unsupported typed artifact kind {payload_kind!r}")
        else:
            _unknown_fields(payload, expected_fields, f"{label}.payload", errors)
        if payload.get("schema_version") != "1.0.0":
            errors.append(f"{label}.payload.schema_version: expected 1.0.0")
        if payload.get("captured_at") != item.get("captured_at"):
            errors.append(f"{label}: payload captured_at must match artifact metadata")
        if not _valid_timestamp(payload.get("captured_at")):
            errors.append(f"{label}.payload.captured_at: must be offset-aware RFC3339")
    return artifacts, payloads


def _require_artifact_kind(
    artifact_ids: Any,
    required_kind: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    where: str,
    errors: list[str],
) -> None:
    ids = _string_list(artifact_ids)
    if ids is None:
        errors.append(f"{where}: artifact_ids must be a list of non-empty strings")
        return
    found = False
    for artifact_id in ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            errors.append(f"{where}: unknown artifact_id {artifact_id!r}")
        elif artifact.get("kind") == required_kind:
            found = True
    if not found:
        errors.append(f"{where}: requires referenced {required_kind} artifact")


def _referenced_payloads(
    artifact_ids: Any,
    required_kind: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    payloads: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    ids = _string_list(artifact_ids) or []
    return [
        payloads[artifact_id]
        for artifact_id in ids
        if artifact_id in payloads and artifacts.get(artifact_id, {}).get("kind") == required_kind
    ]


def _validate_not_run(client: Mapping[str, Any], label: str, errors: list[str]) -> None:
    scalar_fields = (
        "client_version",
        "authority_endpoint_identity_sha256",
        "transport",
        "auth_registration_path",
        "oauth_metadata_discovered",
    )
    if any(client.get(field) is not None for field in scalar_fields):
        errors.append(f"{label}: not_run observations must be null")
    for field in ("tools_discovered", "tools_list_artifact_ids", "calls"):
        if client.get(field) != []:
            errors.append(f"{label}: not_run {field} must be empty")

    scope_mapping = _mapping(client.get("scope_mapping"))
    if scope_mapping is not None and scope_mapping.get("observed") is not None:
        errors.append(f"{label}: not_run scope_mapping.observed must be null")
    lifecycle = _mapping(client.get("session_lifecycle"))
    if lifecycle is not None:
        observed = {"post", "get", "delete", "session_id_observed", "session_fingerprint_sha256"}
        if any(lifecycle.get(field) is not None for field in observed) or lifecycle.get("artifact_ids") != []:
            errors.append(f"{label}: not_run session observations must be null/empty")
    readback = _mapping(client.get("external_read_back"))
    if readback is not None:
        values = set(_READBACK_FIELDS) - {"status", "artifact_ids"}
        if readback.get("status") != "not_run" or any(readback.get(field) is not None for field in values):
            errors.append(f"{label}: not_run external read-back observations must be null")
        if readback.get("artifact_ids") != []:
            errors.append(f"{label}: not_run external read-back artifacts must be empty")
    evidence = _mapping(client.get("evidence"))
    if evidence is not None and (evidence.get("captured_at") is not None or evidence.get("artifacts") != []):
        errors.append(f"{label}: not_run evidence must be null/empty")


def _validate_passed(
    client: Mapping[str, Any],
    *,
    label: str,
    authority_hash: Any,
    artifacts: Mapping[str, Mapping[str, Any]],
    artifact_payloads: Mapping[str, Mapping[str, Any]],
    errors: list[str],
) -> None:
    client_id = client.get("client_id")
    version = client.get("client_version")
    transport = client.get("transport")
    auth_path = client.get("auth_registration_path")
    if not _is_observed_text(version):
        errors.append(f"{label}.client_version: passed requires a real non-placeholder version")
    if not _is_observed_text(transport):
        errors.append(f"{label}.transport: passed requires an observed transport")
    if not _is_observed_text(auth_path):
        errors.append(f"{label}.auth_registration_path: passed requires an observed auth path")
    if client.get("oauth_metadata_discovered") is not True:
        errors.append(f"{label}.oauth_metadata_discovered: passed requires true with evidence")
    client_authority = client.get("authority_endpoint_identity_sha256")
    if not _is_hash(client_authority) or client_authority != authority_hash:
        errors.append(f"{label}: passed client authority identity must match matrix authority")

    identity_payloads = [
        payload
        for artifact_id, payload in artifact_payloads.items()
        if artifacts.get(artifact_id, {}).get("kind") == "client_identity"
    ]
    identity_ok = any(
        payload.get("client_id") == client_id
        and payload.get("client_class") == client.get("client_class")
        and payload.get("client_version") == version
        for payload in identity_payloads
    )
    if not identity_ok:
        errors.append(f"{label}.client_version: typed client_identity artifact does not match client")

    tools = _string_list(client.get("tools_discovered"))
    if tools is None or not MVP_TOOLS.issubset(tools):
        errors.append(f"{label}.tools_discovered: passed requires all MVP tools")
    _require_artifact_kind(
        client.get("tools_list_artifact_ids"),
        "tools_list_trace",
        artifacts,
        f"{label}.tools_list_artifact_ids",
        errors,
    )
    tools_payloads = _referenced_payloads(
        client.get("tools_list_artifact_ids"), "tools_list_trace", artifacts, artifact_payloads
    )
    for payload in tools_payloads:
        if payload.get("client_id") != client_id or payload.get("client_version") != version:
            errors.append(f"{label}: tools trace client/version does not match matrix")
        if payload.get("authority_endpoint_identity_sha256") != authority_hash:
            errors.append(f"{label}: tools trace authority does not match matrix")
        if payload.get("transport") != transport:
            errors.append(f"{label}: tools trace transport does not match matrix")
        if payload.get("auth_registration_path") != auth_path:
            errors.append(f"{label}: tools trace auth path does not match matrix")
        if payload.get("oauth_metadata_discovered") is not True:
            errors.append(f"{label}: tools trace must prove OAuth metadata discovery")
        traced_tools = _string_list(payload.get("tools"))
        if traced_tools is None or tools is None or set(traced_tools) != set(tools):
            errors.append(f"{label}: tools trace tools do not match matrix")

    scope_mapping = _mapping(client.get("scope_mapping"))
    required_scope: Any = None
    if scope_mapping is None:
        errors.append(f"{label}.scope_mapping: passed requires an exact observed scope")
    else:
        required_scope = scope_mapping.get("required_exact_scope")
        observed_scope = scope_mapping.get("observed")
        if (
            not _is_observed_text(required_scope)
            or not _is_observed_text(observed_scope)
            or required_scope != observed_scope
        ):
            errors.append(f"{label}.scope_mapping: observed must exactly equal required_exact_scope")

    lifecycle = _mapping(client.get("session_lifecycle"))
    if lifecycle is None:
        errors.append(f"{label}.session_lifecycle: must be an object")
        return
    for field, method in (("post", "POST"), ("get", "GET"), ("delete", "DELETE")):
        if lifecycle.get(field) is not True:
            errors.append(f"{label}.session_lifecycle: passed requires {method}=true")
    if lifecycle.get("session_id_observed") is not True:
        errors.append(f"{label}.session_lifecycle: passed requires observed Mcp-Session-Id")
    fingerprint = lifecycle.get("session_fingerprint_sha256")
    if not _is_hash(fingerprint):
        errors.append(f"{label}.session_lifecycle: requires a SHA-256 session fingerprint")
    for field, method in (("post", "POST"), ("get", "GET"), ("delete", "DELETE")):
        kind = f"session_{field}_trace"
        _require_artifact_kind(lifecycle.get("artifact_ids"), kind, artifacts, label, errors)
        for payload in _referenced_payloads(
            lifecycle.get("artifact_ids"), kind, artifacts, artifact_payloads
        ):
            if payload.get("client_id") != client_id:
                errors.append(f"{label}: session trace client does not match matrix")
            if payload.get("authority_endpoint_identity_sha256") != authority_hash:
                errors.append(f"{label}: session trace authority does not match matrix")
            if payload.get("session_fingerprint_sha256") != fingerprint:
                errors.append(f"{label}: session trace fingerprint does not match lifecycle")
            if payload.get("method") != method or payload.get("success") is not True:
                errors.append(f"{label}: {method} session trace does not prove success")

    calls = _list(client.get("calls"))
    successful = 0
    if calls is None:
        errors.append(f"{label}.calls: must be a list")
        calls = []
    for index, raw in enumerate(calls):
        call_label = f"{label}.calls[{index}]"
        call = _mapping(raw)
        if call is None:
            errors.append(f"{call_label}: must be an object")
            continue
        _unknown_fields(call, _CALL_FIELDS, call_label, errors)
        if call.get("outcome") == "passed":
            successful += 1
        if not _valid_timestamp(call.get("captured_at")):
            errors.append(f"{call_label}.captured_at: must be offset-aware RFC3339")
        if call.get("authority_endpoint_identity_sha256") != authority_hash:
            errors.append(f"{call_label}: authority identity mismatch")
        if call.get("session_fingerprint_sha256") != fingerprint:
            errors.append(f"{call_label}: session fingerprint does not match lifecycle")
        _require_artifact_kind(call.get("artifact_ids"), "tool_call_trace", artifacts, call_label, errors)
        traces = _referenced_payloads(
            call.get("artifact_ids"), "tool_call_trace", artifacts, artifact_payloads
        )
        if not traces:
            continue
        for payload in traces:
            if payload.get("client_id") != client_id:
                errors.append(f"{call_label}: tool call trace client does not match matrix")
            if payload.get("authority_endpoint_identity_sha256") != authority_hash:
                errors.append(f"{call_label}: tool call trace authority does not match matrix")
            if payload.get("session_fingerprint_sha256") != fingerprint:
                errors.append(f"{call_label}: tool call trace session does not match lifecycle")
            if payload.get("tool") != call.get("tool") or payload.get("outcome") != call.get("outcome"):
                errors.append(f"{call_label}: tool call trace outcome does not match matrix")
            if not _is_observed_text(payload.get("request_id")):
                errors.append(f"{call_label}: tool call trace requires an observed request_id")
            if payload.get("scope") != required_scope:
                errors.append(f"{call_label}: tool call trace scope does not match required exact scope")
    if successful < 1:
        errors.append(f"{label}.calls: passed requires at least one successful tool call")


def _validate_readback(
    client: Mapping[str, Any],
    *,
    label: str,
    authority_hash: Any,
    artifacts: Mapping[str, Mapping[str, Any]],
    artifact_payloads: Mapping[str, Mapping[str, Any]],
    artifact_root: Path | None,
    client_by_id: Mapping[str, Mapping[str, Any]],
    errors: list[str],
) -> bool:
    readback = _mapping(client.get("external_read_back"))
    if readback is None:
        errors.append(f"{label}.external_read_back: must be an object")
        return False
    _unknown_fields(readback, _READBACK_FIELDS, f"{label}.external_read_back", errors)
    status = readback.get("status")
    if status not in ALLOWED_STATUS:
        errors.append(f"{label}.external_read_back.status: invalid status")
        return False
    if status != "passed":
        return False

    writer = client.get("client_id")
    reader = readback.get("reader_client_id")
    reader_client: Mapping[str, Any] | None = None
    if not _is_observed_text(reader) or reader == writer:
        errors.append(f"{label}.external_read_back: reader must be a different client")
    elif reader not in client_by_id:
        errors.append(f"{label}.external_read_back: reader client is absent from matrix")
    else:
        reader_client = client_by_id[reader]
        if reader_client.get("status") != "passed":
            errors.append(f"{label}.external_read_back: reader client itself is not passed")
    if readback.get("authority_endpoint_identity_sha256") != authority_hash:
        errors.append(f"{label}.external_read_back: authority identity mismatch")
    if not _is_observed_text(readback.get("memory_id")):
        errors.append(f"{label}.external_read_back.memory_id: required")
    revision = readback.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append(f"{label}.external_read_back.revision: must be a positive integer")
    if not _is_hash(readback.get("content_sha256")):
        errors.append(f"{label}.external_read_back.content_sha256: must be SHA-256")
    _require_artifact_kind(
        readback.get("artifact_ids"),
        "external_read_back_trace",
        artifacts,
        f"{label}.external_read_back",
        errors,
    )
    traces = _referenced_payloads(
        readback.get("artifact_ids"),
        "external_read_back_trace",
        artifacts,
        artifact_payloads,
    )
    if not traces:
        return False

    scope_mapping = _mapping(client.get("scope_mapping"))
    scope = scope_mapping.get("observed") if scope_mapping is not None else None
    writer_call_ids = {
        artifact_id
        for call in (_list(client.get("calls")) or [])
        if isinstance(call, Mapping)
        for artifact_id in (_string_list(call.get("artifact_ids")) or [])
    }

    reader_artifacts: dict[str, Mapping[str, Any]] = {}
    reader_payloads: dict[str, Mapping[str, Any]] = {}
    reader_call_ids: set[str] = set()
    if reader_client is not None:
        reader_evidence = _mapping(reader_client.get("evidence"))
        if reader_evidence is None:
            errors.append(f"{label}.external_read_back: reader evidence is missing")
        else:
            reader_artifacts, reader_payloads = _validate_artifacts(
                reader_evidence,
                client_label=f"reader({reader!r})",
                artifact_root=artifact_root,
                errors=errors,
            )
        reader_call_ids = {
            artifact_id
            for call in (_list(reader_client.get("calls")) or [])
            if isinstance(call, Mapping)
            for artifact_id in (_string_list(call.get("artifact_ids")) or [])
        }

    for trace in traces:
        if trace.get("writer_client_id") != writer:
            errors.append(f"{label}.external_read_back: read-back trace writer does not match matrix")
        if trace.get("reader_client_id") != reader:
            errors.append(f"{label}.external_read_back: read-back trace reader does not match matrix")
        if trace.get("authority_endpoint_identity_sha256") != authority_hash:
            errors.append(f"{label}.external_read_back: read-back trace authority does not match matrix")
        if trace.get("scope") != scope:
            errors.append(f"{label}.external_read_back: read-back trace scope does not match matrix")
        if trace.get("memory_id") != readback.get("memory_id"):
            errors.append(f"{label}.external_read_back: read-back trace memory ID does not match matrix")
        if trace.get("revision") != readback.get("revision"):
            errors.append(f"{label}.external_read_back: read-back trace revision does not match matrix")
        if trace.get("content_sha256") != readback.get("content_sha256"):
            errors.append(f"{label}.external_read_back: read-back trace content hash does not match matrix")
        if trace.get("outcome") != "passed":
            errors.append(f"{label}.external_read_back: read-back trace outcome must be passed")

        writer_call_id = trace.get("writer_call_artifact_id")
        writer_call = artifact_payloads.get(str(writer_call_id))
        if writer_call_id not in writer_call_ids or writer_call is None:
            errors.append(f"{label}.external_read_back: writer call trace is not referenced by writer")
        elif (
            artifacts.get(str(writer_call_id), {}).get("kind") != "tool_call_trace"
            or writer_call.get("client_id") != writer
            or writer_call.get("tool") not in {"memory_add", "memory_replace"}
            or writer_call.get("outcome") != "passed"
            or writer_call.get("authority_endpoint_identity_sha256") != authority_hash
            or writer_call.get("scope") != scope
            or writer_call.get("memory_id") != readback.get("memory_id")
            or writer_call.get("revision") != readback.get("revision")
            or writer_call.get("content_sha256") != readback.get("content_sha256")
        ):
            errors.append(f"{label}.external_read_back: writer call trace does not prove the same memory")

        reader_call_id = trace.get("reader_call_artifact_id")
        reader_call = reader_payloads.get(str(reader_call_id))
        if reader_call_id not in reader_call_ids or reader_call is None:
            errors.append(f"{label}.external_read_back: reader call trace is not referenced by reader")
        elif (
            reader_artifacts.get(str(reader_call_id), {}).get("kind") != "tool_call_trace"
            or reader_call.get("client_id") != reader
            or reader_call.get("tool") not in {"memory_get", "memory_search"}
            or reader_call.get("outcome") != "passed"
            or reader_call.get("authority_endpoint_identity_sha256") != authority_hash
            or reader_call.get("scope") != scope
            or reader_call.get("memory_id") != readback.get("memory_id")
            or reader_call.get("revision") != readback.get("revision")
            or reader_call.get("content_sha256") != readback.get("content_sha256")
        ):
            errors.append(f"{label}.external_read_back: reader call trace does not prove the same memory")
    return True


def validate_matrix_document(
    document: Any,
    *,
    artifact_root: str | Path | None,
) -> MatrixValidationReport:
    """Validate one parsed matrix and read back every referenced artifact.

    The function never raises for an invalid matrix; all defects are returned in
    ``errors``.  ``artifact_root`` is mandatory as soon as any evidence artifact
    exists so a syntactically plausible digest cannot stand in for a real file.
    """

    errors: list[str] = []
    root = _mapping(document)
    if root is None:
        return MatrixValidationReport(("matrix: root must be an object",), (), ())
    _unknown_fields(root, _ROOT_FIELDS, "matrix", errors)
    if root.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"matrix.schema_version: expected {SCHEMA_VERSION}")
    if not _valid_timestamp(root.get("generated_at")):
        errors.append("matrix.generated_at: must be offset-aware RFC3339")

    authority = _mapping(root.get("authority"))
    authority_hash: Any = None
    if authority is None:
        errors.append("matrix.authority: must be an object")
    else:
        _unknown_fields(authority, _AUTHORITY_FIELDS, "matrix.authority", errors)
        authority_hash = authority.get("endpoint_identity_sha256")
        mode = authority.get("deployment_mode")
        if mode == "not_configured":
            for field in ("endpoint", "endpoint_identity_sha256", "server_version"):
                if authority.get(field) is not None:
                    errors.append(f"matrix.authority.{field}: must be null when not_configured")
        elif not _is_hash(authority_hash):
            errors.append("matrix.authority.endpoint_identity_sha256: configured authority requires SHA-256")

    contract = _mapping(root.get("evidence_contract"))
    required_client_ids: list[str] = []
    if contract is None:
        errors.append("matrix.evidence_contract: must be an object")
    else:
        _unknown_fields(contract, _CONTRACT_FIELDS, "matrix.evidence_contract", errors)
        if contract.get("allowed_status") != ["not_run", "blocked", "failed", "passed"]:
            errors.append("matrix.evidence_contract.allowed_status: contract drift")
        if contract.get("unknown_values_must_be_null") is not True:
            errors.append("matrix.evidence_contract.unknown_values_must_be_null: must be true")
        if contract.get("secrets_forbidden") is not True:
            errors.append("matrix.evidence_contract.secrets_forbidden: must be true")
        parsed_required = _string_list(contract.get("required_client_ids"))
        if parsed_required is None or len(parsed_required) != len(set(parsed_required)):
            errors.append("matrix.evidence_contract.required_client_ids: must be unique non-empty strings")
        else:
            required_client_ids = parsed_required

    clients_raw = _list(root.get("clients"))
    if clients_raw is None:
        errors.append("matrix.clients: must be a list")
        clients_raw = []
    clients: list[Mapping[str, Any]] = []
    client_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(clients_raw):
        label = f"matrix.clients[{index}]"
        client = _mapping(raw)
        if client is None:
            errors.append(f"{label}: must be an object")
            continue
        clients.append(client)
        _unknown_fields(client, _CLIENT_FIELDS, label, errors)
        client_id = client.get("client_id")
        if not _is_observed_text(client_id):
            errors.append(f"{label}.client_id: must be non-placeholder text")
            continue
        assert isinstance(client_id, str)
        if client_id in client_by_id:
            errors.append(f"matrix.clients: duplicate client_id {client_id!r}")
        else:
            client_by_id[client_id] = client

    missing_clients = sorted(set(required_client_ids) - set(client_by_id))
    if missing_clients:
        errors.append(f"matrix.clients: missing required clients: {', '.join(missing_clients)}")

    passed: list[str] = []
    read_back: list[str] = []
    artifact_root_path = Path(artifact_root) if artifact_root is not None else None
    for index, client in enumerate(clients):
        client_id = client.get("client_id")
        label = f"matrix.clients[{index}]({client_id!r})"
        client_error_start = len(errors)
        status = client.get("status")
        if status not in ALLOWED_STATUS:
            errors.append(f"{label}.status: invalid status")

        scope = client.get("scope_mapping")
        if scope is not None:
            scope_mapping = _mapping(scope)
            if scope_mapping is None:
                errors.append(f"{label}.scope_mapping: must be null or an object")
            else:
                _unknown_fields(scope_mapping, _SCOPE_FIELDS, f"{label}.scope_mapping", errors)

        lifecycle = _mapping(client.get("session_lifecycle"))
        if lifecycle is None:
            errors.append(f"{label}.session_lifecycle: must be an object")
        else:
            _unknown_fields(lifecycle, _SESSION_FIELDS, f"{label}.session_lifecycle", errors)
        readback = _mapping(client.get("external_read_back"))
        if readback is None:
            errors.append(f"{label}.external_read_back: must be an object")
        else:
            _unknown_fields(readback, _READBACK_FIELDS, f"{label}.external_read_back", errors)
        evidence = _mapping(client.get("evidence"))
        if evidence is None:
            errors.append(f"{label}.evidence: must be an object")
            artifacts: dict[str, Mapping[str, Any]] = {}
            artifact_payloads: dict[str, Mapping[str, Any]] = {}
        else:
            artifacts, artifact_payloads = _validate_artifacts(
                evidence,
                client_label=label,
                artifact_root=artifact_root_path,
                errors=errors,
            )

        if status == "not_run":
            _validate_not_run(client, label, errors)
        elif status == "passed":
            _validate_passed(
                client,
                label=label,
                authority_hash=authority_hash,
                artifacts=artifacts,
                artifact_payloads=artifact_payloads,
                errors=errors,
            )
            if len(errors) == client_error_start and isinstance(client_id, str):
                passed.append(client_id)
        elif status in {"blocked", "failed"}:
            if evidence is None or not _valid_timestamp(evidence.get("captured_at")) or not artifacts:
                errors.append(f"{label}: {status} requires timestamped hashed evidence")

        readback_valid = _validate_readback(
            client,
            label=label,
            authority_hash=authority_hash,
            artifacts=artifacts,
            artifact_payloads=artifact_payloads,
            artifact_root=artifact_root_path,
            client_by_id=client_by_id,
            errors=errors,
        )
        if readback_valid and len(errors) == client_error_start and isinstance(client_id, str):
            read_back.append(client_id)

    _scan_matrix_values(root, "matrix", errors)
    unique_errors = tuple(dict.fromkeys(errors))
    if unique_errors:
        return MatrixValidationReport(unique_errors, (), ())
    return MatrixValidationReport(unique_errors, tuple(passed), tuple(read_back))


def validate_matrix_file(
    path: str | Path,
    *,
    artifact_root: str | Path | None = None,
) -> MatrixValidationReport:
    """Load and validate a matrix; malformed JSON becomes a normal error."""

    matrix_path = Path(path)
    try:
        document = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return MatrixValidationReport((f"matrix file unreadable: {type(exc).__name__}",), (), ())
    return validate_matrix_document(
        document,
        artifact_root=artifact_root if artifact_root is not None else matrix_path.parent,
    )


def main(argv: list[str] | None = None) -> int:
    """Validate a matrix non-interactively and emit one JSON report.

    Exit 0 means contract/hash/leak/semantic validation passed.  It does not
    mean Gate 7 or Gate 8 passed; callers must inspect the explicit pass lists.
    """

    parser = argparse.ArgumentParser(
        prog="python -m recall_memory_mcp.interop_evidence",
        description=(
            "Validate typed interop evidence. A valid all-not_run matrix is not "
            "a real-host or cross-client E2E pass."
        ),
    )
    parser.add_argument("matrix", help="interop matrix JSON")
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="root for relative evidence paths (defaults to matrix directory)",
    )
    args = parser.parse_args(argv)
    report = validate_matrix_file(args.matrix, artifact_root=args.artifact_root)
    print(
        json.dumps(
            {
                "ok": report.ok,
                "errors": list(report.errors),
                "passed_client_ids": list(report.passed_client_ids),
                "external_read_back_client_ids": list(report.external_read_back_client_ids),
            },
            indent=2,
        )
    )
    return 0 if report.ok else 1


__all__ = [
    "ALLOWED_STATUS",
    "MVP_TOOLS",
    "SCHEMA_VERSION",
    "MatrixValidationReport",
    "main",
    "validate_matrix_document",
    "validate_matrix_file",
]


if __name__ == "__main__":  # pragma: no cover - exercised via main() and release shell smoke
    raise SystemExit(main())
