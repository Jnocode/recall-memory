"""Phase 8 task 8.1 — the cross-client canary scope and content.

Task 8.1 asks for *one* canary scope and content that carries no personal data
and no secret.  "We wrote a nice string" is not evidence, so this module pins
the four properties that actually make the canary safe and usable:

1. **Unique** — two independently built plans never share a scope, an
   idempotency key, or a content body, so a re-run can never read back the
   previous run's row and call it a pass.
2. **Legal** — every field validates against the real strict request models in
   ``recall_memory_mcp.models``.  A canary the server would reject is useless.
3. **Clean** — the content survives the project's own ``redact_text`` byte for
   byte (nothing in it looks like a secret), and an independent PII/secret
   scanner with a proven-hostile negative control finds nothing.
4. **Durable** — the plan serialises to JSON and reads back identical, which is
   what lets steps 8.2 - 8.10 be driven from an artifact instead of memory.

Nothing here talks to a network or to a real database.
"""

from __future__ import annotations

import getpass
import hashlib
import json

import pytest
from pydantic import ValidationError

from recall_memory_mcp import canary as canary_mod
from recall_memory_mcp import models
from recall_memory_mcp.redaction import redact_text
from recall_memory_mcp.canary import (
    CANARY_KIND,
    CANARY_SCHEMA_VERSION,
    CANARY_SCOPE_PREFIX,
    CanaryPlan,
    build_canary_plan,
    scan_sensitive,
)

from _support import FakeMemoryRow, FakeRepository, envelope, make_context, make_server


@pytest.fixture()
def plan() -> CanaryPlan:
    return build_canary_plan()


# --------------------------------------------------------------------------
# 8.1 — one scope, and it is unique per run
# --------------------------------------------------------------------------


def test_scope_matches_the_documented_project_pattern(plan):
    assert plan.scope.startswith(CANARY_SCOPE_PREFIX)
    assert len(plan.scope) <= models.MAX_SCOPE_CHARS
    # The canary must be addressable by a client, so it has to satisfy the very
    # same pattern the tools enforce -- `legacy:unscoped` style scopes are out.
    models.GetRequest(memory_id="mem-1", scope=plan.scope)


def test_scope_is_a_project_scope_not_global(plan):
    """A canary in `global` would pollute the user's real shared memory."""

    assert plan.scope != "global"
    assert plan.scope.startswith("project:")


def test_two_plans_never_collide(plan):
    others = [build_canary_plan() for _ in range(32)]
    scopes = {plan.scope} | {other.scope for other in others}
    assert len(scopes) == 33

    keys = set()
    for candidate in [plan, *others]:
        keys.update(
            {
                candidate.add.idempotency_key,
                candidate.replace.idempotency_key,
                candidate.remove.idempotency_key,
            }
        )
    assert len(keys) == 33 * 3

    contents = {c.add.content for c in [plan, *others]}
    assert len(contents) == 33


def test_run_id_is_lowercase_hex_and_long_enough_to_not_collide(plan):
    assert plan.run_id == plan.run_id.lower()
    assert len(plan.run_id) >= 12
    assert set(plan.run_id) <= set("0123456789abcdef")


def test_explicit_run_id_is_reproducible():
    a = build_canary_plan(run_id="0123456789ab", clock=lambda: "2026-08-07T00:00:00+00:00")
    b = build_canary_plan(run_id="0123456789ab", clock=lambda: "2026-08-07T00:00:00+00:00")
    assert a == b
    assert a.to_json() == b.to_json()


def test_rejects_a_run_id_that_would_break_the_scope_pattern():
    for bad in ("", "UPPER1234567", "has space12", "semi;colon12", "../../etc12"):
        with pytest.raises(ValueError):
            build_canary_plan(run_id=bad)


# --------------------------------------------------------------------------
# 8.1 — no personal data, no secret
# --------------------------------------------------------------------------

CANARY_TEXTS = ("add", "replace")


@pytest.mark.parametrize("step", CANARY_TEXTS)
def test_content_survives_the_projects_own_redactor_unchanged(plan, step):
    """If `redact_text` changes a byte, the content looked like a secret."""

    content = getattr(plan, step).content
    assert redact_text(content) == content


@pytest.mark.parametrize("step", CANARY_TEXTS)
def test_content_has_no_sensitive_findings(plan, step):
    assert scan_sensitive(getattr(plan, step).content) == ()


def test_scope_kind_tags_and_keys_are_also_scanned(plan):
    findings = plan.sensitive_findings()
    assert findings == {}, findings


def test_local_operator_identity_never_leaks_into_the_canary(plan):
    """The canary ships in an artifact; the host username must not ride along."""

    username = getpass.getuser()
    blob = plan.to_json()
    assert username.lower() not in blob.lower()
    assert plan.sensitive_findings(extra_terms=(username,)) == {}


def test_digest_masking_does_not_blind_the_digit_run_detector():
    """The hex-digest exemption must not become a bypass.

    A sha256 is masked (it contains `a-f`), a pure-digit identifier is not.
    """

    digest = hashlib.sha256(b"canary").hexdigest()
    assert "long_digit_run" not in scan_sensitive(digest)
    assert "long_digit_run" in scan_sensitive("1234567890123456")
    assert "long_digit_run" in scan_sensitive("employee 987654321 record")
    # Uppercase hex is not the digest shape, so it stays in scope.
    assert "long_digit_run" in scan_sensitive("ref 1234567890 ABCDEF")


def test_digest_masking_is_needed_for_a_hash_with_leading_digits():
    """Regression guard: a digest starting with 9+ digits is still clean."""

    assert scan_sensitive("9876543210abcdef" + "0" * 48) == ()


def test_content_is_printable_ascii(plan):
    for step in CANARY_TEXTS:
        content = getattr(plan, step).content
        assert content.isascii()
        assert content.isprintable()


def test_scanner_negative_control_catches_every_detector():
    """A scanner that never fires would make the tests above meaningless."""

    hostile = {
        "email": "ping me at alice.smith@example.com ok",
        "phone": "call +886912345678 now",
        "long_digit_run": "id 123456789012 here",
        "ipv4": "host 192.168.13.37 here",
        "windows_path": r"see C:\Users\someone\recall.db",
        "unc_path": r"share \\fileserver\memories",
        "posix_system_path": "db at /home/someone/.recall/recall.db",
        "url_with_credentials": "https://user:hunter2@example.com/x",
        "jwt": "eyJhbGciOi.eyJzdWIiOj.QssW9Zr8pQ",
        "bearer": "Authorization Bearer abcdef123456",
        "private_key_block": "-----BEGIN RSA PRIVATE KEY-----",
        "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
        "github_token": "ghp_0123456789abcdef0123456789abcdef0123",
        "slack_token": "xoxb-0123456789-abcdefghij",
        "openai_key": "sk-0123456789abcdefghij",
        "kv_secret": "client_secret=swordfish",
        "non_ascii": "canary \u00e9\u00e9\u00e9",
    }
    for detector, text in hostile.items():
        findings = scan_sensitive(text)
        assert detector in findings, f"{detector} not detected in {text!r}"


def test_scanner_extra_terms_are_case_insensitive():
    assert "extra_term" in scan_sensitive("hello JUN world", extra_terms=("jun",))
    assert scan_sensitive("hello world", extra_terms=("jun",)) == ()


def test_scanner_is_clean_on_the_control_string():
    assert scan_sensitive("Recall phase 8 canary, synthetic data only.") == ()


# --------------------------------------------------------------------------
# 8.1 — the plan is legal input for the real tools
# --------------------------------------------------------------------------


def test_requests_validate_against_the_real_strict_models(plan):
    add = plan.add_request()
    assert isinstance(add, models.AddRequest)
    assert add.scope == plan.scope
    assert add.kind == CANARY_KIND
    assert add.idempotency_key == plan.add.idempotency_key

    replace = plan.replace_request("mem-canary")
    assert isinstance(replace, models.ReplaceRequest)
    assert replace.expected_revision == 1
    assert replace.content == plan.replace.content

    remove = plan.remove_request("mem-canary")
    assert isinstance(remove, models.RemoveRequest)
    assert remove.expected_revision == 2


def test_the_three_writes_use_three_different_idempotency_keys(plan):
    keys = {plan.add.idempotency_key, plan.replace.idempotency_key, plan.remove.idempotency_key}
    assert len(keys) == 3
    for key in keys:
        assert len(key) >= models.MIN_IDEMPOTENCY_KEY_CHARS
        assert len(key) <= models.MAX_IDEMPOTENCY_KEY_CHARS


def test_revision_ladder_matches_tasks_8_2_to_8_6(plan):
    assert plan.add.expected_revision_after == 1
    assert plan.replace.expected_revision == 1
    assert plan.replace.expected_revision_after == 2
    assert plan.remove.expected_revision == 2
    assert plan.remove.expected_revision_after == 3


def test_content_hashes_are_real_sha256_of_the_content(plan):
    for step in CANARY_TEXTS:
        entry = getattr(plan, step)
        assert entry.content_sha256 == hashlib.sha256(entry.content.encode("utf-8")).hexdigest()
    assert plan.add.content_sha256 != plan.replace.content_sha256


def test_replace_content_is_distinguishable_from_add_content(plan):
    """8.5 requires proving the *old* content is gone, so they must differ."""

    assert plan.add.content != plan.replace.content
    assert "revision 1" in plan.add.content
    assert "revision 2" in plan.replace.content


# --------------------------------------------------------------------------
# 8.1 — artifact round trip
# --------------------------------------------------------------------------


def test_json_round_trip_is_lossless(plan, tmp_path):
    path = tmp_path / "phase8-canary.json"
    path.write_text(plan.to_json(), encoding="utf-8")
    reloaded = CanaryPlan.from_json(path.read_text(encoding="utf-8"))
    assert reloaded == plan


def test_artifact_declares_its_schema_version_and_is_pure_json(plan):
    document = json.loads(plan.to_json())
    assert document["schema_version"] == CANARY_SCHEMA_VERSION
    assert document["scope"] == plan.scope


def test_artifact_carries_no_owner_actor_or_credential_fields(plan):
    document = json.loads(plan.to_json())
    flat = json.dumps(document).lower()
    for banned in ("owner_id", "actor_id", "grant_id", "token", "secret", "password", "db_path"):
        assert banned not in flat, banned


def test_plan_rejects_unknown_fields():
    document = json.loads(build_canary_plan().to_json())
    document["owner_id"] = "owner-local"
    with pytest.raises(ValidationError):
        CanaryPlan.from_json(json.dumps(document))


def test_plan_is_frozen(plan):
    with pytest.raises(ValidationError):
        plan.scope = "global"


# --------------------------------------------------------------------------
# 8.1 — the canary actually drives the real tools (server-side proof only)
# --------------------------------------------------------------------------


def _canary_server(plan: CanaryPlan):
    repository = FakeRepository(
        rows=[
            FakeMemoryRow(
                memory_id="mem-canary",
                content=plan.add.content,
                scope=plan.scope,
                kind=CANARY_KIND,
                tags=plan.tags,
            )
        ]
    )
    ctx = make_context(memory_scopes=(plan.scope,))
    return make_server(repository, ctx=ctx)


def test_canary_plan_drives_add_replace_remove_through_the_real_tools(plan):
    """Server-side rehearsal of 8.2 / 8.4 / 8.6.

    This is NOT the cross-client E2E: one in-process client is not three real
    hosts.  It only proves the canary payload is accepted by the shipped tool
    surface, so a failure during the manual run means the *host*, not the
    canary.
    """

    server, repository = _canary_server(plan)

    added = envelope(server, "memory_add", plan.add_arguments())
    assert added["ok"] is True
    assert added["data"]["revision"] == plan.add.expected_revision_after
    memory_id = added["data"]["memory_id"]

    replaced = envelope(server, "memory_replace", plan.replace_arguments(memory_id))
    assert replaced["ok"] is True
    assert replaced["data"]["revision"] == plan.replace.expected_revision_after

    removed = envelope(server, "memory_remove", plan.remove_arguments(memory_id))
    assert removed["ok"] is True
    assert removed["data"]["revision"] == plan.remove.expected_revision_after

    written = dict(repository.calls)
    assert written["add_memory"]["content"] == plan.add.content
    assert written["add_memory"]["scope"] == plan.scope
    assert written["add_memory"]["idempotency_key"] == plan.add.idempotency_key
    assert written["replace_memory"]["content"] == plan.replace.content
    assert written["replace_memory"]["idempotency_key"] == plan.replace.idempotency_key
    assert written["remove_memory"]["idempotency_key"] == plan.remove.idempotency_key


def test_canary_scope_is_isolated_from_other_scopes(plan):
    """A grant holding only the canary scope must not reach `global`."""

    server, repository = _canary_server(plan)
    arguments = dict(plan.add_arguments())
    arguments["scope"] = "global"
    data = envelope(server, "memory_add", arguments)
    assert data["ok"] is False
    assert repository.wrote_anything() is False


def test_module_exports_are_stable():
    for name in (
        "CANARY_KIND",
        "CANARY_SCHEMA_VERSION",
        "CANARY_SCOPE_PREFIX",
        "CANARY_TAGS",
        "CanaryPlan",
        "build_canary_plan",
        "scan_sensitive",
    ):
        assert name in canary_mod.__all__, name
        assert hasattr(canary_mod, name), name
