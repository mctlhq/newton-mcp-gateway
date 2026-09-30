"""Tests for `newton_mcp.runtime.auth`: header resolution and redaction.

Every test injects its own `env` mapping (never `monkeypatch.setenv`) since
`resolve_auth_header` takes one explicitly -- the whole point of that seam.
"""

from __future__ import annotations

import pytest

from newton_mcp.runtime.auth import MissingAuthSecret, redact, resolve_auth_header
from newton_mcp.runtime.config import HttpAuth

_SENTINEL = "sk-live-super-secret-DO-NOT-LEAK-0123456789"  # noqa: S105 - test sentinel, not a real credential


# ---------------------------------------------------------------------------
# T6: resolve_auth_header
# ---------------------------------------------------------------------------


def test_resolve_with_scheme_returns_scheme_prefixed_value() -> None:
    auth = HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")
    name, value = resolve_auth_header(auth, server_name="alice", env={"ALICE_MCP_TOKEN": _SENTINEL})
    assert (name, value) == ("Authorization", f"Bearer {_SENTINEL}")


def test_resolve_without_scheme_returns_bare_value() -> None:
    auth = HttpAuth(header="X-Api-Key", scheme=None, env="ALICE_MCP_TOKEN")
    name, value = resolve_auth_header(auth, server_name="alice", env={"ALICE_MCP_TOKEN": _SENTINEL})
    assert (name, value) == ("X-Api-Key", _SENTINEL)


def test_unset_variable_raises_missing_auth_secret_naming_variable_and_server() -> None:
    auth = HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")
    with pytest.raises(MissingAuthSecret) as excinfo:
        resolve_auth_header(auth, server_name="alice", env={})
    message = str(excinfo.value)
    assert "ALICE_MCP_TOKEN" in message
    assert "alice" in message


@pytest.mark.parametrize("blank_value", ["", "   ", "\t\t"])
def test_blank_variable_raises_missing_auth_secret(blank_value: str) -> None:
    auth = HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")
    with pytest.raises(MissingAuthSecret, match="ALICE_MCP_TOKEN"):
        resolve_auth_header(auth, server_name="alice", env={"ALICE_MCP_TOKEN": blank_value})


def test_missing_auth_secret_message_never_contains_a_nonblank_value() -> None:
    auth = HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")
    with pytest.raises(MissingAuthSecret) as excinfo:
        resolve_auth_header(auth, server_name="alice", env={})
    assert _SENTINEL not in str(excinfo.value)


def test_missing_auth_secret_is_not_a_value_error() -> None:
    assert not issubclass(MissingAuthSecret, ValueError)


# ---------------------------------------------------------------------------
# R2: control characters, unencodable values, and surrounding whitespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_char", ["\r", "\n", "\r\n", "\x00", "\x7f"])
def test_control_character_in_value_is_rejected_without_echo(bad_char: str) -> None:
    auth = HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")
    poisoned = f"{_SENTINEL}{bad_char}evil"
    with pytest.raises(MissingAuthSecret) as excinfo:
        resolve_auth_header(auth, server_name="alice", env={"ALICE_MCP_TOKEN": poisoned})
    message = str(excinfo.value)
    assert _SENTINEL not in message
    assert "evil" not in message


def test_unencodable_value_is_rejected_without_echo() -> None:
    auth = HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")
    poisoned = f"{_SENTINEL}-☃"  # a snowman: not latin-1 encodable
    with pytest.raises(MissingAuthSecret) as excinfo:
        resolve_auth_header(auth, server_name="alice", env={"ALICE_MCP_TOKEN": poisoned})
    assert _SENTINEL not in str(excinfo.value)


def test_nonblank_value_with_surrounding_whitespace_is_preserved_byte_exact() -> None:
    auth = HttpAuth(header="X-Api-Key", scheme=None, env="ALICE_MCP_TOKEN")
    padded = f"  {_SENTINEL}  "
    name, value = resolve_auth_header(auth, server_name="alice", env={"ALICE_MCP_TOKEN": padded})
    assert (name, value) == ("X-Api-Key", padded)


# ---------------------------------------------------------------------------
# T6: redact()
# ---------------------------------------------------------------------------


def test_redact_replaces_every_occurrence_of_the_secret() -> None:
    text = f"connect failed: header carried {_SENTINEL} in the request ({_SENTINEL})"
    redacted = redact(text, [_SENTINEL])
    assert _SENTINEL not in redacted
    assert "[redacted]" in redacted


@pytest.mark.parametrize("blank_secret", ["", "   ", None])
def test_redact_is_a_noop_for_an_empty_or_blank_secret(blank_secret: str | None) -> None:
    text = "some diagnostic text that must survive untouched"
    secrets = [] if blank_secret is None else [blank_secret]
    assert redact(text, secrets) == text
