"""Redaction tests (task 2.6 / 2.8).

R8.5: logs and errors must never carry absolute DB paths, tracebacks,
tokens or credential-bearing URLs.
"""

from __future__ import annotations

import pytest

from recall_memory_mcp import redaction


MARK = redaction.REDACTED


def _assert_scrubbed(raw: str, *secrets: str) -> str:
    cleaned = redaction.redact_text(raw)
    for secret in secrets:
        assert secret not in cleaned, f"{secret!r} survived redaction: {cleaned!r}"
    assert MARK in cleaned
    return cleaned


# --------------------------------------------------------------------------
# bearer / authorization headers
# --------------------------------------------------------------------------


def test_bearer_token_is_redacted() -> None:
    _assert_scrubbed(
        "Authorization: Bearer abcDEF123456ghiJKL7890",
        "abcDEF123456ghiJKL7890",
    )


def test_basic_credentials_header_is_redacted() -> None:
    _assert_scrubbed("authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA==")


def test_bearer_keyword_survives_so_operators_can_still_read_the_log() -> None:
    cleaned = redaction.redact_text("Authorization: Bearer abcDEF123456ghiJKL7890")
    assert "Bearer" in cleaned


# --------------------------------------------------------------------------
# assignment-style secrets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("api_key=sk-live-0123456789abcdef", "sk-live-0123456789abcdef"),
        ("API-KEY: sk-live-0123456789abcdef", "sk-live-0123456789abcdef"),
        ("token = ghp_0123456789abcdefghijklmnopqrstuvwxyz", "ghp_0123456789abcdef"),
        ('{"client_secret": "s3cr3t-value-0123456789"}', "s3cr3t-value-0123456789"),
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("refresh_token=rt_0123456789abcdefgh", "rt_0123456789abcdefgh"),
    ],
)
def test_assignment_style_secrets_are_redacted(raw: str, secret: str) -> None:
    _assert_scrubbed(raw, secret)


@pytest.mark.parametrize(
    "raw",
    [
        "sk-proj-0123456789abcdefghij",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "xoxb-0123456789-0123456789-abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_well_known_token_prefixes_are_redacted_even_bare(raw: str) -> None:
    cleaned = redaction.redact_text(f"leaked {raw} here")
    assert raw not in cleaned
    assert MARK in cleaned


def test_jwt_is_redacted() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    _assert_scrubbed(f"got token {jwt}", jwt)


# --------------------------------------------------------------------------
# credential-bearing URLs
# --------------------------------------------------------------------------


def test_url_credentials_are_redacted_but_host_is_kept() -> None:
    cleaned = _assert_scrubbed(
        "connect https://alice:s3cr3tpass@auth.example.com/mcp",
        "s3cr3tpass",
        "alice:s3cr3tpass",
    )
    assert "auth.example.com" in cleaned


def test_postgres_style_url_credentials_are_redacted() -> None:
    _assert_scrubbed("postgresql://user:p4ssw0rd@db.internal:5432/recall", "p4ssw0rd")


def test_query_string_token_is_redacted() -> None:
    _assert_scrubbed(
        "GET https://example.com/mcp?access_token=abc123456789def&x=1",
        "abc123456789def",
    )


def test_plain_https_url_without_credentials_is_left_alone() -> None:
    raw = "see https://modelcontextprotocol.io/docs for details"
    assert redaction.redact_text(raw) == raw


# --------------------------------------------------------------------------
# filesystem paths (R8.5: never return absolute DB paths)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\Users\Jun\.hermes\recall.db",
        r"c:/Users/Jun/AppData/Roaming/recall-memory-mcp/authority.db",
        r"D:\Workspace\03_Dev_Projects\recall\recall.db",
        r"\\fileserver\share\recall.db",
    ],
)
def test_windows_paths_are_redacted(raw: str) -> None:
    cleaned = redaction.redact_text(f"failed to open {raw}")
    assert "Jun" not in cleaned
    assert "Workspace" not in cleaned
    assert "fileserver" not in cleaned
    assert MARK in cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "/home/jun/.hermes/recall.db",
        "/Users/jun/Library/Application Support/recall/authority.db",
        "/var/lib/recall/authority.db",
        "/tmp/pytest-of-jun/test_x0/authority.db",
    ],
)
def test_posix_home_and_system_paths_are_redacted(raw: str) -> None:
    cleaned = redaction.redact_text(f"sqlite3.OperationalError: unable to open {raw}")
    assert raw not in cleaned
    assert MARK in cleaned


def test_relative_words_that_merely_contain_slashes_are_not_destroyed() -> None:
    raw = "use the memory_search tool with scope project:recall"
    assert redaction.redact_text(raw) == raw


def test_https_scheme_is_not_mistaken_for_a_windows_drive() -> None:
    raw = "endpoint https://127.0.0.1:8765/mcp is healthy"
    assert redaction.redact_text(raw) == raw


# --------------------------------------------------------------------------
# tracebacks
# --------------------------------------------------------------------------


TRACEBACK = '''Traceback (most recent call last):
  File "C:\\Users\\Jun\\recall\\src\\recall\\mcp_repository.py", line 217, in add_memory
    conn.execute(sql, params)
sqlite3.IntegrityError: UNIQUE constraint failed: mcp_idempotency.idempotency_key'''


def test_traceback_body_never_survives() -> None:
    cleaned = redaction.redact_text(f"boom\n{TRACEBACK}")
    assert "File " not in cleaned
    assert "mcp_repository.py" not in cleaned
    assert "line 217" not in cleaned
    assert "Jun" not in cleaned
    assert "boom" in cleaned
    assert MARK in cleaned


def test_redact_exception_returns_class_free_of_arguments() -> None:
    try:
        raise sqlite_error()
    except Exception as exc:  # noqa: BLE001
        message = redaction.redact_exception(exc)
    assert "recall.db" not in message
    assert "Jun" not in message


def sqlite_error() -> Exception:
    return OSError(r"unable to open database file C:\Users\Jun\.hermes\recall.db")


# --------------------------------------------------------------------------
# structured payloads
# --------------------------------------------------------------------------


def test_sensitive_keys_are_redacted_regardless_of_value_shape() -> None:
    payload = {
        "authorization": "Bearer abc123456789",
        "db_path": r"C:\Users\Jun\.hermes\recall.db",
        "nested": {"client_secret": "shhh-0123456789", "safe": "hello"},
        "list": [{"token": "tok-0123456789"}, "plain text"],
        "count": 3,
    }
    cleaned = redaction.redact_mapping(payload)
    flat = repr(cleaned)
    for secret in ("abc123456789", "Jun", "shhh-0123456789", "tok-0123456789"):
        assert secret not in flat
    assert cleaned["nested"]["safe"] == "hello"
    assert cleaned["count"] == 3


def test_redaction_does_not_mutate_the_input_mapping() -> None:
    payload = {"token": "tok-0123456789"}
    redaction.redact_mapping(payload)
    assert payload["token"] == "tok-0123456789"


def test_redaction_is_idempotent() -> None:
    once = redaction.redact_text(f"Bearer abc123456789 at {TRACEBACK}")
    assert redaction.redact_text(once) == once


def test_non_string_input_is_handled_without_raising() -> None:
    assert redaction.redact_text(None) == ""
    assert redaction.redact_text(12345) == "12345"
