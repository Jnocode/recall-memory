"""Host client-config tests (tasks 6.5, 6.6).

`client-config` prints a template by default.  Only an explicit `--write`
may touch a host configuration file, and only after a backup plus a JSON
read-back (R9.5).  Templates never contain a secret value: credentials are
environment placeholders (verification architecture item 6).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from recall_memory_mcp import cli, client_configs
from recall_memory_mcp.server import WRITE_TOOL_NAMES


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
        "RECALL_MCP_CONFIG_DIR": str(tmp_path / "authority"),
    }


def run(argv, env):
    import io

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), env=dict(env), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# template rendering
# ---------------------------------------------------------------------------


def test_supported_clients_match_the_spec(isolated_env) -> None:
    assert client_configs.CLIENTS == ("chatgpt", "claude", "kiro")


@pytest.mark.parametrize("client", client_configs.CLIENTS)
def test_every_client_renders_a_non_empty_template(client, isolated_env) -> None:
    code, out, err = run(["client-config", client], isolated_env)
    assert code == cli.EXIT_OK, err
    assert out.strip()


def test_unknown_client_is_a_usage_error(isolated_env) -> None:
    code, _out, err = run(["client-config", "emacs"], isolated_env)
    assert code == cli.EXIT_USAGE
    assert err.strip()


def test_kiro_template_is_valid_json_with_the_server_url(isolated_env) -> None:
    code, out, _err = run(["client-config", "kiro"], isolated_env)
    assert code == cli.EXIT_OK

    payload = json.loads(out)
    entry = payload["mcpServers"][client_configs.SERVER_KEY]
    assert entry["url"] == "http://127.0.0.1:8765/mcp"
    assert entry["disabled"] is False


def test_kiro_template_uses_an_environment_placeholder_for_credentials(isolated_env) -> None:
    payload = json.loads(run(["client-config", "kiro"], isolated_env)[1])
    entry = payload["mcpServers"][client_configs.SERVER_KEY]
    assert entry["headers"]["Authorization"] == f"Bearer ${{{client_configs.TOKEN_ENV_VAR}}}"


def test_kiro_auto_approve_only_lists_read_only_tools(isolated_env) -> None:
    payload = json.loads(run(["client-config", "kiro"], isolated_env)[1])
    approved = set(payload["mcpServers"][client_configs.SERVER_KEY]["autoApprove"])
    assert approved == set(client_configs.READ_ONLY_TOOLS)
    assert approved.isdisjoint(WRITE_TOOL_NAMES)


def test_kiro_template_follows_the_public_url_in_remote_mode(isolated_env) -> None:
    env = dict(isolated_env)
    env.update(
        {
            "RECALL_MCP_MODE": "remote",
            "RECALL_MCP_PUBLIC_URL": "https://memory.example.test",
            "RECALL_MCP_OAUTH_ISSUER": "https://auth.example.test",
            "RECALL_MCP_ALLOWED_HOSTS": "memory.example.test",
        }
    )
    payload = json.loads(run(["client-config", "kiro"], env)[1])
    entry = payload["mcpServers"][client_configs.SERVER_KEY]
    assert entry["url"] == "https://memory.example.test/mcp"


@pytest.mark.parametrize("client", client_configs.CLIENTS)
def test_no_template_ever_contains_a_secret_value(client, isolated_env) -> None:
    env = dict(isolated_env)
    env["RECALL_MCP_STATIC_API_KEY"] = "sk-template-must-not-leak-abcdefgh"
    env["RECALL_MCP_OAUTH_CLIENT_SECRET"] = "template-client-secret-value"

    _code, out, _err = run(["client-config", client], env)
    assert "sk-template-must-not-leak" not in out
    assert "template-client-secret-value" not in out


@pytest.mark.parametrize("client", ("chatgpt", "claude"))
def test_ui_configured_hosts_render_guidance_not_json(client, isolated_env) -> None:
    template = client_configs.render(client, cli.resolve_settings(isolated_env))
    assert template.format == "markdown"
    assert template.writable is False
    assert "http://127.0.0.1:8765/mcp" in template.text


@pytest.mark.parametrize("client", ("chatgpt", "claude"))
def test_write_is_refused_for_ui_configured_hosts(client, tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "should-not-exist.json"
    code, _out, err = run(["client-config", client, "--write", str(target)], isolated_env)

    assert code == cli.EXIT_REFUSED
    assert not target.exists()
    assert "connector" in err.lower() or "ui" in err.lower()


# ---------------------------------------------------------------------------
# --write behaviour
# ---------------------------------------------------------------------------


def test_without_write_flag_no_file_is_created(tmp_path: Path, isolated_env) -> None:
    before = set(tmp_path.rglob("*"))
    code, out, _err = run(["client-config", "kiro"], isolated_env)
    assert code == cli.EXIT_OK
    assert out.strip()
    assert set(tmp_path.rglob("*")) == before


def test_write_creates_a_new_host_config(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "kiro" / "settings" / "mcp.json"
    code, out, err = run(["client-config", "kiro", "--write", str(target)], isolated_env)

    assert code == cli.EXIT_OK, err
    written = json.loads(target.read_text(encoding="utf-8"))
    assert client_configs.SERVER_KEY in written["mcpServers"]
    assert "read-back" in out.lower()


def test_write_backs_up_an_existing_file_byte_for_byte(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {"other": {"url": "http://example.test/mcp"}}}, indent=2)
    target.write_text(original, encoding="utf-8")

    code, out, err = run(["client-config", "kiro", "--write", str(target)], isolated_env)
    assert code == cli.EXIT_OK, err

    backups = sorted(tmp_path.glob("mcp.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert "backup" in out.lower()


def test_write_preserves_unrelated_servers(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"url": "http://example.test/mcp"}}}),
        encoding="utf-8",
    )
    assert run(["client-config", "kiro", "--write", str(target)], isolated_env)[0] == cli.EXIT_OK

    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"] == {"url": "http://example.test/mcp"}
    assert written["mcpServers"][client_configs.SERVER_KEY]["url"] == "http://127.0.0.1:8765/mcp"


def test_write_preserves_unrelated_top_level_keys(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"telemetry": False, "mcpServers": {}}), encoding="utf-8")
    assert run(["client-config", "kiro", "--write", str(target)], isolated_env)[0] == cli.EXIT_OK

    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["telemetry"] is False


def test_write_replaces_our_own_previous_entry_idempotently(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    assert run(["client-config", "kiro", "--write", str(target)], isolated_env)[0] == cli.EXIT_OK
    first = target.read_text(encoding="utf-8")
    assert run(["client-config", "kiro", "--write", str(target)], isolated_env)[0] == cli.EXIT_OK

    assert target.read_text(encoding="utf-8") == first
    assert len(sorted(tmp_path.glob("mcp.json.bak-*"))) == 1


def test_write_refuses_a_target_that_is_not_json(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    target.write_text("this is not json {", encoding="utf-8")

    code, _out, err = run(["client-config", "kiro", "--write", str(target)], isolated_env)
    assert code == cli.EXIT_REFUSED
    assert target.read_text(encoding="utf-8") == "this is not json {"
    assert "json" in err.lower()


def test_write_refuses_a_target_whose_root_is_not_an_object(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")

    code, _out, err = run(["client-config", "kiro", "--write", str(target)], isolated_env)
    assert code == cli.EXIT_REFUSED
    assert target.read_text(encoding="utf-8") == "[1, 2, 3]"
    assert err.strip()


def test_write_refuses_a_symlinked_target(tmp_path: Path, isolated_env) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privilege on Windows
        pytest.skip("symlink creation is not permitted in this environment")

    code, _out, err = run(["client-config", "kiro", "--write", str(link)], isolated_env)
    assert code == cli.EXIT_REFUSED
    assert real.read_text(encoding="utf-8") == "{}"
    assert "symlink" in err.lower()


def test_write_refuses_a_directory_target(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "a-directory"
    target.mkdir()
    code, _out, err = run(["client-config", "kiro", "--write", str(target)], isolated_env)
    assert code == cli.EXIT_REFUSED
    assert err.strip()


def test_write_restores_the_backup_when_read_back_fails(
    tmp_path: Path, isolated_env, monkeypatch
) -> None:
    target = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {"other": {"url": "http://example.test/mcp"}}}, indent=2)
    target.write_text(original, encoding="utf-8")

    def corrupt(path: Path, payload: dict) -> None:  # noqa: ANN001
        path.write_text("{corrupted", encoding="utf-8")

    monkeypatch.setattr(client_configs, "_atomic_write_json", corrupt)
    code, _out, err = run(["client-config", "kiro", "--write", str(target)], isolated_env)

    assert code == cli.EXIT_FAILURE
    assert target.read_text(encoding="utf-8") == original
    assert err.strip()


def test_write_output_does_not_echo_a_secret(tmp_path: Path, isolated_env) -> None:
    env = dict(isolated_env)
    env["RECALL_MCP_STATIC_API_KEY"] = "sk-write-must-not-leak-abcdefgh"
    target = tmp_path / "mcp.json"
    _code, out, err = run(["client-config", "kiro", "--write", str(target)], env)

    assert "sk-write-must-not-leak" not in out
    assert "sk-write-must-not-leak" not in err
    assert "sk-write-must-not-leak" not in target.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_written_host_config_is_owner_only_on_posix(tmp_path: Path, isolated_env) -> None:
    target = tmp_path / "mcp.json"
    assert run(["client-config", "kiro", "--write", str(target)], isolated_env)[0] == cli.EXIT_OK
    assert target.stat().st_mode & 0o777 == 0o600
