"""Settings tests (task 2.5 / 2.8).

R7.1 (Host/Origin allowlist), R7.5 (no secrets in defaults/repr),
R9.3 (serve binds 127.0.0.1 by default), R8.5 (no absolute paths in dumps).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recall_memory_mcp import redaction
from recall_memory_mcp.settings import ServerMode, ServerSettings, SettingsError


def _env(**overrides: str) -> dict[str, str]:
    base = {"APPDATA": r"C:\Users\testuser\AppData\Roaming", "HOME": "/home/testuser"}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# defaults (R9.3)
# --------------------------------------------------------------------------


def test_defaults_bind_loopback_in_local_mode() -> None:
    settings = ServerSettings.from_env(_env())
    assert settings.host == "127.0.0.1"
    assert settings.mode is ServerMode.LOCAL
    assert 1 <= settings.port <= 65535


def test_default_paths_never_point_at_the_private_hermes_database() -> None:
    settings = ServerSettings.from_env(_env())
    db = str(settings.db_path).replace("\\", "/").lower()
    assert ".hermes" not in db
    assert not db.endswith("/recall.db")
    assert "recall-memory-mcp" in db
    assert settings.config_path.parent == settings.config_dir


def test_config_dir_follows_appdata_on_windows_style_env() -> None:
    settings = ServerSettings.from_env(_env())
    assert Path(r"C:\Users\testuser\AppData\Roaming") in settings.config_dir.parents


def test_config_dir_falls_back_to_home_when_appdata_is_absent() -> None:
    env = _env()
    env.pop("APPDATA")
    settings = ServerSettings.from_env(env)
    assert "testuser" in str(settings.config_dir)


# --------------------------------------------------------------------------
# no secret defaults (R7.5)
# --------------------------------------------------------------------------


def test_no_secret_bearing_attribute_has_a_non_empty_default() -> None:
    settings = ServerSettings.from_env(_env())
    for name in ("oauth_client_secret", "static_api_key"):
        assert getattr(settings, name) is None, name


def test_secrets_are_only_read_from_the_environment_never_hardcoded() -> None:
    settings = ServerSettings.from_env(
        _env(RECALL_MCP_STATIC_API_KEY="local-test-key-0123456789", RECALL_MCP_MODE="local")
    )
    assert settings.static_api_key == "local-test-key-0123456789"
    assert "local-test-key-0123456789" not in repr(settings)
    assert "local-test-key-0123456789" not in str(settings)


def test_redacted_dump_hides_secrets_and_absolute_paths() -> None:
    settings = ServerSettings.from_env(_env(RECALL_MCP_STATIC_API_KEY="local-test-key-0123456789"))
    dumped = settings.redacted_dump()
    flat = repr(dumped)
    assert "local-test-key-0123456789" not in flat
    assert "testuser" not in flat
    assert redaction.REDACTED in flat
    assert dumped["host"] == "127.0.0.1"
    assert dumped["mode"] == "local"


def test_repr_contains_no_absolute_path() -> None:
    settings = ServerSettings.from_env(_env())
    text = repr(settings)
    assert "testuser" not in text
    assert "AppData" not in text


# --------------------------------------------------------------------------
# allowlists (R7.1)
# --------------------------------------------------------------------------


def test_local_allowlists_cover_loopback_names_with_the_actual_port() -> None:
    settings = ServerSettings.from_env(_env(RECALL_MCP_PORT="8899"))
    assert "127.0.0.1:8899" in settings.allowed_hosts
    assert "localhost:8899" in settings.allowed_hosts
    assert "http://127.0.0.1:8899" in settings.allowed_origins
    assert "http://localhost:8899" in settings.allowed_origins


def test_wildcard_is_never_an_allowlist_entry() -> None:
    settings = ServerSettings.from_env(_env())
    assert "*" not in settings.allowed_hosts
    assert "*" not in settings.allowed_origins


def test_explicit_wildcard_in_the_environment_is_rejected() -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(_env(RECALL_MCP_ALLOWED_ORIGINS="*"))
    with pytest.raises(SettingsError):
        ServerSettings.from_env(_env(RECALL_MCP_ALLOWED_HOSTS="*"))


def test_remote_allowlist_entries_from_env_are_parsed_and_trimmed() -> None:
    settings = ServerSettings.from_env(
        _env(
            RECALL_MCP_MODE="remote",
            RECALL_MCP_HOST="0.0.0.0",
            RECALL_MCP_PUBLIC_URL="https://memory.example.com/mcp",
            RECALL_MCP_ALLOWED_HOSTS=" memory.example.com , mcp.example.com ",
            RECALL_MCP_ALLOWED_ORIGINS="https://memory.example.com",
            RECALL_MCP_OAUTH_ISSUER="https://auth.example.com",
        )
    )
    assert settings.allowed_hosts == ("memory.example.com", "mcp.example.com")
    assert settings.require_auth is True


# --------------------------------------------------------------------------
# authority memory-scope allowlist (R1 / IDE project scope)
# --------------------------------------------------------------------------


def test_memory_scopes_default_to_global_only() -> None:
    settings = ServerSettings.from_env(_env())
    assert settings.memory_scopes == ("global",)


def test_memory_scopes_are_explicit_trimmed_and_deduplicated() -> None:
    settings = ServerSettings.from_env(
        _env(
            RECALL_MCP_MEMORY_SCOPES=(
                " global, project:recall-ide-e2e, project:recall-ide-e2e, project:other "
            )
        )
    )
    assert settings.memory_scopes == (
        "global",
        "project:recall-ide-e2e",
        "project:other",
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "*",
        "project:*",
        "legacy:unscoped",
        "project:Uppercase",
        "global,,project:recall",
        ",global",
        "global,",
    ],
)
def test_invalid_memory_scope_allowlist_fails_closed(value: str) -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(_env(RECALL_MCP_MEMORY_SCOPES=value))


def test_memory_scope_allowlist_accepts_its_documented_maximum() -> None:
    value = ",".join(f"project:p{index}" for index in range(64))
    settings = ServerSettings.from_env(_env(RECALL_MCP_MEMORY_SCOPES=value))
    assert len(settings.memory_scopes) == 64


def test_memory_scope_allowlist_rejects_more_than_its_documented_maximum() -> None:
    value = ",".join(f"project:p{index}" for index in range(65))
    with pytest.raises(SettingsError, match="at most 64"):
        ServerSettings.from_env(_env(RECALL_MCP_MEMORY_SCOPES=value))


# --------------------------------------------------------------------------
# mode invariants (R1.4 / R7.6 / R9.3)
# --------------------------------------------------------------------------


def test_remote_mode_requires_https_public_url() -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(
            _env(
                RECALL_MCP_MODE="remote",
                RECALL_MCP_PUBLIC_URL="http://memory.example.com/mcp",
                RECALL_MCP_ALLOWED_HOSTS="memory.example.com",
                RECALL_MCP_OAUTH_ISSUER="https://auth.example.com",
            )
        )


def test_remote_mode_requires_an_oauth_issuer() -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(
            _env(
                RECALL_MCP_MODE="remote",
                RECALL_MCP_PUBLIC_URL="https://memory.example.com/mcp",
                RECALL_MCP_ALLOWED_HOSTS="memory.example.com",
            )
        )


def test_remote_mode_cannot_disable_auth() -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(
            _env(
                RECALL_MCP_MODE="remote",
                RECALL_MCP_PUBLIC_URL="https://memory.example.com/mcp",
                RECALL_MCP_ALLOWED_HOSTS="memory.example.com",
                RECALL_MCP_OAUTH_ISSUER="https://auth.example.com",
                RECALL_MCP_REQUIRE_AUTH="false",
            )
        )


def test_remote_mode_requires_an_explicit_allowlist() -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(
            _env(
                RECALL_MCP_MODE="remote",
                RECALL_MCP_PUBLIC_URL="https://memory.example.com/mcp",
                RECALL_MCP_OAUTH_ISSUER="https://auth.example.com",
            )
        )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_local_mode_must_bind_loopback(host: str) -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(_env(RECALL_MCP_HOST=host))


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(_env(RECALL_MCP_MODE="yolo"))


# --------------------------------------------------------------------------
# transport limits (R7 / design 6.4)
# --------------------------------------------------------------------------


def test_body_and_timeout_limits_have_positive_defaults() -> None:
    settings = ServerSettings.from_env(_env())
    assert settings.max_body_bytes > 0
    assert settings.tool_timeout_seconds > 0
    assert settings.max_concurrent_calls > 0


@pytest.mark.parametrize(
    "override",
    [
        {"RECALL_MCP_MAX_BODY_BYTES": "0"},
        {"RECALL_MCP_MAX_BODY_BYTES": "-1"},
        {"RECALL_MCP_MAX_BODY_BYTES": "not-a-number"},
        {"RECALL_MCP_PORT": "0"},
        {"RECALL_MCP_PORT": "70000"},
        {"RECALL_MCP_TOOL_TIMEOUT_SECONDS": "0"},
    ],
)
def test_invalid_numeric_settings_fail_closed(override: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env(_env(**override))


def test_settings_are_frozen() -> None:
    settings = ServerSettings.from_env(_env())
    with pytest.raises(Exception):
        settings.host = "0.0.0.0"  # type: ignore[misc]


def test_stateless_http_is_not_available_in_this_contract() -> None:
    settings = ServerSettings.from_env(_env())
    assert settings.stateful_http is True
