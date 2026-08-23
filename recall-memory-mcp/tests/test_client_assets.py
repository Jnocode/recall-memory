"""Host integration assets — Phase 7 tasks 7.1, 7.2, 7.5, 7.8 and 7.12.

These assets are the part of the project a user copies and pastes, so they are
the part most likely to rot silently: a tool gets renamed, the default port
changes, someone pastes a real token into an example. Nothing here can prove
that a real ChatGPT/Claude/Kiro instance connects (that is tasks 7.3-7.11a, and
it needs the real hosts), but everything that *can* be checked mechanically is
checked here:

* every documented JSON snippet actually parses;
* the Kiro example matches the schema Kiro documents, and matches what
  ``client_configs.kiro_payload`` generates;
* ``autoApprove`` never contains a write tool;
* no asset contains a credential, an absolute path, or a private DB path;
* every tool name mentioned in an asset is a tool the server really exposes,
  and every tool the server exposes is documented;
* the per-agent config keys that differ between IDEs (VS Code's ``servers``
  root, Claude Code's mandatory ``type``) are exactly as the vendors document.

Vendor sources for the prose are recorded, with SHA-256 digests, in
``artifacts/recall-mcp-cross-client/evidence/phase7-doc-sources.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from recall_memory_mcp import client_configs as cc
from recall_memory_mcp.server import TOOL_NAMES, WRITE_TOOL_NAMES
from recall_memory_mcp.settings import ServerSettings

DIST_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DIST_ROOT.parent
CLIENTS_DIR = DIST_ROOT / "clients"

CHATGPT_README = CLIENTS_DIR / "chatgpt" / "README.md"
CHATGPT_INSTRUCTIONS = CLIENTS_DIR / "chatgpt" / "PLUGIN_INSTRUCTIONS.md"
CLAUDE_README = CLIENTS_DIR / "claude" / "README.md"
CLAUDE_INSTRUCTIONS = CLIENTS_DIR / "claude" / "INSTRUCTIONS.md"
KIRO_README = CLIENTS_DIR / "kiro" / "README.md"
KIRO_EXAMPLE = CLIENTS_DIR / "kiro" / "mcp.example.json"
KIRO_STEERING = CLIENTS_DIR / "kiro" / "recall-memory-steering.example.md"
IDE_DOC = REPO_ROOT / "docs" / "ide-mcp-setup.md"

ALL_ASSETS = (
    CHATGPT_README,
    CHATGPT_INSTRUCTIONS,
    CLAUDE_README,
    CLAUDE_INSTRUCTIONS,
    KIRO_README,
    KIRO_EXAMPLE,
    KIRO_STEERING,
    IDE_DOC,
)

MARKDOWN_ASSETS = tuple(path for path in ALL_ASSETS if path.suffix == ".md")

#: True when the tests run from the git checkout rather than from an unpacked
#: sdist.  `clients/` is grafted into the sdist (MANIFEST.in) so those checks
#: always run; `docs/ide-mcp-setup.md` lives above the distribution root and is
#: only present in a checkout.
IN_REPO_CHECKOUT = (REPO_ROOT / ".kiro").is_dir() or (REPO_ROOT / ".git").exists()


def _require(path: Path) -> None:
    """Skip only outside a checkout — inside one, a missing asset is a failure."""

    if path.is_file():
        return
    if IN_REPO_CHECKOUT:
        pytest.fail(f"{path} is missing from this checkout")
    pytest.skip(f"{path.name} lives outside the distribution root; run from a repo checkout")

#: Keys Kiro documents for an ``mcpServers`` entry.  Anything else is either a
#: typo or something we invented, and both are bugs.
KIRO_ENTRY_KEYS = frozenset(
    {
        "command",
        "args",
        "env",
        "url",
        "headers",
        "oauth",
        "oauthScopes",
        "disabled",
        "autoApprove",
        "disabledTools",
    }
)
KIRO_OAUTH_KEYS = frozenset({"clientId", "clientSecret", "redirectUri", "oauthScopes"})

#: Header / field names whose value must never be a literal in a shipped asset.
CREDENTIAL_FIELDS = ("Authorization", "clientSecret", "clientId", "x-api-key", "x-auth-token")

_ENV_PLACEHOLDER = re.compile(r"\$\{[A-Z0-9_]+\}")
_JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.DOTALL)

# Real-looking credentials.  Deliberately narrow: it must not fire on prose.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
)

# Absolute paths that would leak a maintainer's machine layout.
_ABSOLUTE_PATHS = (
    re.compile(r"[A-Za-z]:\\\\?[Uu]sers\\"),
    re.compile(r"[A-Za-z]:[/\\]Workspace"),
    re.compile(r"/home/[a-z]"),
    re.compile(r"/Users/[A-Za-z]"),
)


def _read(path: Path) -> str:
    _require(path)
    return path.read_text(encoding="utf-8")


def _json_blocks(path: Path) -> list[tuple[int, str]]:
    """Return ``(index, source)`` for every fenced ```json block in a document."""

    return list(enumerate(_JSON_BLOCK.findall(_read(path))))


def _default_settings() -> ServerSettings:
    return ServerSettings.from_env({})


# ---------------------------------------------------------------------------
# presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_ASSETS, ids=lambda p: p.name)
def test_asset_exists_and_is_substantive(path: Path) -> None:
    _require(path)
    assert len(_read(path).strip()) > 400, f"{path.name} is a stub"


def test_design_declared_layout_is_present() -> None:
    """design.md section 5 names these exact files; do not silently rename them."""

    for relative in (
        "chatgpt/README.md",
        "chatgpt/PLUGIN_INSTRUCTIONS.md",
        "claude/README.md",
        "claude/INSTRUCTIONS.md",
        "kiro/mcp.example.json",
        "kiro/recall-memory-steering.example.md",
    ):
        assert (CLIENTS_DIR / relative).is_file(), relative


# ---------------------------------------------------------------------------
# every documented snippet must actually parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", MARKDOWN_ASSETS, ids=lambda p: p.name)
def test_every_json_block_parses(path: Path) -> None:
    blocks = _json_blocks(path)
    for index, source in blocks:
        try:
            json.loads(source)
        except json.JSONDecodeError as exc:  # pragma: no cover - failure message
            pytest.fail(f"{path.name} json block #{index} does not parse: {exc}")


def test_ide_doc_has_a_block_for_every_agent() -> None:
    _require(IDE_DOC)
    blocks = [json.loads(src) for _, src in _json_blocks(IDE_DOC)]
    assert len(blocks) >= 5, "expected Cursor, Devin, Cascade, VS Code and Claude Code blocks"
    roots = [set(block) for block in blocks]
    assert {"servers"} in roots, "the VS Code example must use the `servers` root key"
    assert {"mcpServers"} in roots, "the other agents use the `mcpServers` root key"


# ---------------------------------------------------------------------------
# Kiro example (task 7.8)
# ---------------------------------------------------------------------------


def test_kiro_example_parses_and_uses_only_documented_keys() -> None:
    payload = json.loads(_read(KIRO_EXAMPLE))
    assert set(payload) == {"mcpServers"}
    servers = payload["mcpServers"]
    assert servers, "the example must define at least one server"

    for name, entry in servers.items():
        unknown = set(entry) - KIRO_ENTRY_KEYS
        assert not unknown, f"{name} uses keys Kiro does not document: {sorted(unknown)}"
        assert "url" in entry, f"{name} must be a remote server (task 7.8)"
        oauth = entry.get("oauth")
        if oauth is not None:
            unknown_oauth = set(oauth) - KIRO_OAUTH_KEYS
            assert not unknown_oauth, f"{name}.oauth: {sorted(unknown_oauth)}"


def test_kiro_example_never_auto_approves_a_write_tool() -> None:
    servers = json.loads(_read(KIRO_EXAMPLE))["mcpServers"]
    for name, entry in servers.items():
        approved = entry.get("autoApprove", [])
        assert "*" in approved is False or "*" not in approved, (
            f"{name} auto-approves every tool; write tools must stay manual"
        )
        assert set(approved) <= set(cc.READ_ONLY_TOOLS), (
            f"{name} auto-approves a non read-only tool: "
            f"{sorted(set(approved) - set(cc.READ_ONLY_TOOLS))}"
        )
        assert not set(approved) & set(WRITE_TOOL_NAMES)
        assert set(approved) <= set(TOOL_NAMES), f"{name} auto-approves a tool that does not exist"


def test_kiro_example_carries_no_literal_credential() -> None:
    raw = _read(KIRO_EXAMPLE)
    servers = json.loads(raw)["mcpServers"]
    for name, entry in servers.items():
        for header, value in entry.get("headers", {}).items():
            if header.lower() in {f.lower() for f in CREDENTIAL_FIELDS}:
                assert _ENV_PLACEHOLDER.search(value), (
                    f"{name}.headers.{header} must reference ${{ENV_VAR}}, got a literal"
                )
        oauth = entry.get("oauth", {})
        for field in ("clientId", "clientSecret"):
            if field in oauth:
                assert _ENV_PLACEHOLDER.search(str(oauth[field])), (
                    f"{name}.oauth.{field} must reference ${{ENV_VAR}}"
                )


def test_kiro_example_urls_are_loopback_http_or_remote_https() -> None:
    servers = json.loads(_read(KIRO_EXAMPLE))["mcpServers"]
    loopback_seen = False
    for name, entry in servers.items():
        url = entry["url"]
        assert url.endswith(cc.MCP_PATH), f"{name} url must end with {cc.MCP_PATH}"
        if url.startswith("http://"):
            assert "127.0.0.1" in url or "localhost" in url, (
                f"{name} uses plain HTTP against a non-loopback host"
            )
            loopback_seen = True
            assert url == cc.server_url(_default_settings()), (
                f"{name} drifted from the default endpoint the server actually binds"
            )
        else:
            assert url.startswith("https://"), f"{name} must use HTTPS when remote"
    assert loopback_seen, "the example should show the loopback authority too"


def test_kiro_example_agrees_with_the_generated_config() -> None:
    """The hand-written example and ``client-config kiro`` must not disagree."""

    generated = cc.kiro_payload(_default_settings())["mcpServers"][cc.SERVER_KEY]
    servers = json.loads(_read(KIRO_EXAMPLE))["mcpServers"]
    local = [e for e in servers.values() if e["url"].startswith("http://")]
    assert local, "no loopback entry to compare against"
    entry = local[0]
    assert entry["url"] == generated["url"]
    assert entry["headers"] == generated["headers"]
    assert sorted(entry["autoApprove"]) == sorted(generated["autoApprove"])


# ---------------------------------------------------------------------------
# no secrets, no absolute paths, no private DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_ASSETS, ids=lambda p: p.name)
def test_asset_contains_no_credential_shape(path: Path) -> None:
    text = _read(path)
    for pattern in _SECRET_SHAPES:
        assert not pattern.search(text), f"{path.name} contains something shaped like a secret"


@pytest.mark.parametrize("path", ALL_ASSETS, ids=lambda p: p.name)
def test_asset_contains_no_absolute_machine_path(path: Path) -> None:
    text = _read(path)
    for pattern in _ABSOLUTE_PATHS:
        assert not pattern.search(text), f"{path.name} leaks an absolute path"


@pytest.mark.parametrize("path", ALL_ASSETS, ids=lambda p: p.name)
def test_asset_never_points_at_a_private_database(path: Path) -> None:
    text = _read(path)
    assert ".hermes" not in text
    assert "recall.db" not in text


# ---------------------------------------------------------------------------
# tool-name drift
# ---------------------------------------------------------------------------

_TOOL_MENTION = re.compile(r"\bmemory_[a-z_]+")

#: ``memory_*`` identifiers that are request/response *fields*, not tools.  They
#: are part of the wire contract, so they are allowed to appear in prose — but
#: the list is closed, so a genuinely misspelled tool name still fails.
NON_TOOL_IDENTIFIERS = frozenset({"memory_id", "memory_ref", "memory_count"})

INSTRUCTION_ASSETS = (CHATGPT_INSTRUCTIONS, CLAUDE_INSTRUCTIONS, KIRO_STEERING)


def _mentioned_tools(path: Path) -> set[str]:
    return set(_TOOL_MENTION.findall(_read(path))) - NON_TOOL_IDENTIFIERS


@pytest.mark.parametrize("path", MARKDOWN_ASSETS, ids=lambda p: p.name)
def test_no_asset_mentions_a_tool_that_does_not_exist(path: Path) -> None:
    unknown = _mentioned_tools(path) - set(TOOL_NAMES)
    assert not unknown, f"{path.name} documents tools the server does not expose: {sorted(unknown)}"


def test_non_tool_identifier_allowlist_does_not_hide_a_real_tool() -> None:
    """Guard the guard: the allowlist must never mask an actual tool name."""

    assert not NON_TOOL_IDENTIFIERS & set(TOOL_NAMES)


@pytest.mark.parametrize("path", INSTRUCTION_ASSETS, ids=lambda p: p.name)
def test_instruction_sheets_document_every_tool(path: Path) -> None:
    missing = set(TOOL_NAMES) - _mentioned_tools(path)
    assert not missing, f"{path.name} omits {sorted(missing)}; the model will never call them"


@pytest.mark.parametrize("path", INSTRUCTION_ASSETS, ids=lambda p: p.name)
def test_instruction_sheets_forbid_bulk_saving_and_require_idempotency(path: Path) -> None:
    text = _read(path).lower()
    assert "idempotency_key" in text, "write tools require an idempotency key; say so"
    assert "expected_revision" in text, "replace/remove need the revision that was read"
    assert "untrusted" in text, "stored content must be labelled untrusted data"
    assert "never" in text and ("transcript" in text or "conversation" in text), (
        "the sheet must explicitly forbid bulk-saving the conversation"
    )


# ---------------------------------------------------------------------------
# host-specific facts the vendors document (and that are easy to get wrong)
# ---------------------------------------------------------------------------


def test_chatgpt_readme_matches_the_no_write_client_config_behaviour() -> None:
    template = cc.render("chatgpt", _default_settings())
    assert template.writable is False
    text = _read(CHATGPT_README)
    assert "--write" in text and "refuses" in text
    assert "Developer mode" in text
    assert "Security and login" in text, "the developer-mode toggle lives under Security and login"
    assert "Secure MCP Tunnel" in text
    assert "/mcp" in text


def test_claude_readme_matches_the_no_write_client_config_behaviour() -> None:
    template = cc.render("claude", _default_settings())
    assert template.writable is False
    text = _read(CLAUDE_README)
    assert "Add custom connector" in text
    assert "Settings" in text and "Connectors" in text
    assert "--write" in text and "refuses" in text


def test_kiro_readme_documents_both_config_locations_and_env_approval() -> None:
    text = _read(KIRO_README)
    assert ".kiro/settings/mcp.json" in text
    assert "~/.kiro/settings/mcp.json" in text
    assert "Mcp Approved Env Vars" in text, "Kiro will not expand unapproved variables"
    assert cc.TOKEN_ENV_VAR in text


def test_kiro_readme_documents_exact_local_project_scope_allowlist() -> None:
    text = _read(KIRO_README)
    assert "RECALL_MCP_MEMORY_SCOPES=global,project:recall" in text
    assert "do not\nuse a broad `project:*` grant" in text


def test_ide_doc_covers_every_agent_with_its_real_config_path() -> None:
    _require(IDE_DOC)
    text = _read(IDE_DOC)
    for needle in (
        ".cursor/mcp.json",
        ".devin/mcp_config.json",
        "~/.codeium/windsurf/mcp_config.json",
        ".vscode/mcp.json",
        ".mcp.json",
        "claude mcp add --transport http",
        "devin mcp add",
        "--add-mcp",
    ):
        assert needle in text, f"docs/ide-mcp-setup.md never mentions {needle}"


def test_ide_doc_gets_the_two_easy_traps_right() -> None:
    _require(IDE_DOC)
    blocks = [json.loads(src) for _, src in _json_blocks(IDE_DOC)]

    vscode = [b for b in blocks if "servers" in b]
    assert vscode, "no VS Code block"
    for block in vscode:
        for name, entry in block["servers"].items():
            assert entry.get("type") == "http", f"VS Code entry {name} needs type=http"

    # Not every agent spells the transport the same way, and asserting one
    # universal rule would be wrong: Cursor and legacy Cascade infer HTTP from
    # the presence of `url`, Devin uses `transport`, Claude Code and VS Code
    # require `type`.  What must hold is that the doc shows each spelling at
    # least once, and warns about the one that fails silently.
    remote_entries = [
        entry
        for block in blocks
        for entry in block.get("mcpServers", {}).values()
        if "url" in entry
    ]
    assert any(e.get("type") == "http" for e in remote_entries), (
        "no `type: http` example — Claude Code and VS Code both require it"
    )
    assert any(e.get("transport") == "http" for e in remote_entries), (
        "no `transport: http` example — that is how the Devin CLI spells it"
    )

    text = _read(IDE_DOC)
    assert 'has a "url" but no "type"' in text, (
        "the doc must quote Claude Code's actual error for a url entry with no type"
    )
    assert "`servers`, not `mcpServers`" in text or "servers`, not `mcpServers" in text, (
        "the doc must call out that VS Code uses a different root key"
    )


def test_every_loopback_endpoint_in_the_docs_is_the_one_we_actually_bind() -> None:
    expected = cc.server_url(_default_settings())
    pattern = re.compile(r"http://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/[\w/.-]*)?")
    for path in ALL_ASSETS:
        if not path.is_file():
            continue
        for found in pattern.findall(_read(path)):
            if found.endswith(cc.MCP_PATH):
                assert found == expected, f"{path.name} points at {found}, server binds {expected}"


def _evidence(name: str) -> Path:
    return Path(
        REPO_ROOT.anchor,
        "Workspace",
        "artifacts",
        "recall-mcp-cross-client",
        "evidence",
        name,
    )


def test_doc_source_provenance_record_is_present_and_complete() -> None:
    """Prose claims must be traceable to a vendor page that was really fetched."""

    record = _evidence("phase7-doc-sources.json")
    if not record.is_file():
        pytest.skip("provenance record lives outside the repo; not present in this checkout")
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["unreachable"] == 0
    assert payload["source_count"] >= 10
    for source in payload["sources"]:
        assert source["http_status"] == 200, source["url"]
        assert len(source["sha256"]) == 64


def test_claim_grounding_record_shows_every_claim_landed_in_a_vendor_page() -> None:
    """Provenance is not grounding.

    ``phase7-doc-sources.json`` only proves a page was downloaded.  A UI path we
    invented would still pass it, and the string assertions above would pass too
    because they only read our own README back to us.  The grounding run asserts
    the other direction: each claim's literal text must occur in the vendor
    document it is attributed to.  It caught ``sample_mcp_http_local``, a
    plausible-looking tunnel sample name that OpenAI has never documented.
    """

    record = _evidence("phase7-claim-grounding.json")
    if not record.is_file():
        pytest.skip("grounding record lives outside the repo; not present in this checkout")
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["failures"] == [], payload["failures"]
    assert payload["verdict"] == "PASS"
    assert payload["claims_checked"] >= 35
    for source in payload["sources"]:
        assert source["http_status"] == 200, source["url"]


# ---------------------------------------------------------------------------
# copy-pasteability
# ---------------------------------------------------------------------------

_SHELL_BLOCK = re.compile(r"```(?:bash|sh|shell|console)\n(.*?)```", re.DOTALL)

#: A `${...}` reference is only useful if it is a legal shell variable name.
#: `${RECA...KEN}` is not, and it shipped: an elided placeholder that pasted
#: straight into a terminal as a broken command.
_BAD_PLACEHOLDER = re.compile(r"\$\{[^}]*[^A-Za-z0-9_}][^}]*\}")


@pytest.mark.parametrize("path", MARKDOWN_ASSETS, ids=lambda p: p.name)
def test_env_placeholders_are_legal_shell_variable_names(path: Path) -> None:
    text = _read(path)
    bad = _BAD_PLACEHOLDER.findall(text)
    assert not bad, f"{path.name} has unusable ${{...}} placeholders: {bad}"


@pytest.mark.parametrize("path", MARKDOWN_ASSETS, ids=lambda p: p.name)
def test_shell_blocks_reference_only_the_documented_token_variable(path: Path) -> None:
    """Every credential placeholder must be the one variable we document."""

    for block in _SHELL_BLOCK.findall(_read(path)):
        for name in re.findall(r"\$\{([A-Za-z0-9_]+)\}", block):
            assert name in {cc.TOKEN_ENV_VAR, "RECALL_MCP_OAUTH_CLIENT_ID"}, (
                f"{path.name} shell block uses undocumented variable ${{{name}}}"
            )
