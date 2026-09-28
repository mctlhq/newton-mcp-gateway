from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from newton_mcp.action.approval import create_approval
from newton_mcp.canonical import sha256_hex
from newton_mcp.runtime.audit import (
    AUDIT_PATH_ENV_VAR,
    REDACTED,
    JsonlAuditSink,
    MemoryAuditSink,
    _MAX_VALUE_CHARS,
    load_audit_sink,
    redact_args,
)
from newton_mcp.runtime.lifecycle import ActionState, new_action_record, transition


# ---------------------------------------------------------------------------
# T13: happy-path audit trail through a JsonlAuditSink
# ---------------------------------------------------------------------------


def test_happy_path_audit_trail(tmp_path: Path, fixed_now: datetime) -> None:
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    record = new_action_record(now=fixed_now)

    record = transition(record, ActionState.AUTHORIZED, "policy said auto", now=fixed_now, sink=sink)
    record = transition(record, ActionState.EXECUTING, "calling tool", now=fixed_now, sink=sink)
    record = transition(record, ActionState.EXECUTED, "tool responded", now=fixed_now, sink=sink)
    record = transition(record, ActionState.VERIFYING, "checking outcome", now=fixed_now, sink=sink)
    transition(record, ActionState.SUCCEEDED, "outcome confirmed", now=fixed_now, sink=sink)

    lines = sink.path.read_text().splitlines()
    assert len(lines) == 5

    expected_path = [
        ("proposed", "authorized"),
        ("authorized", "executing"),
        ("executing", "executed"),
        ("executed", "verifying"),
        ("verifying", "succeeded"),
    ]
    for line, (expected_from, expected_to) in zip(lines, expected_path):
        parsed = json.loads(line)
        assert parsed["observation_id"]
        assert parsed["action_id"]
        assert parsed["tool_call_id"]
        assert parsed["verification_id"]
        assert parsed["from"] == expected_from
        assert parsed["to"] == expected_to
        assert parsed["reason"]
        assert "attempt" in parsed
        assert "at" in parsed


# ---------------------------------------------------------------------------
# T14: append-only -- re-opening never truncates
# ---------------------------------------------------------------------------


def test_reopening_sink_never_truncates(tmp_path: Path, fixed_now: datetime) -> None:
    path = tmp_path / "audit.jsonl"
    record = new_action_record(now=fixed_now)

    first_sink = JsonlAuditSink(path)
    transition(record, ActionState.AUTHORIZED, "first", now=fixed_now, sink=first_sink)

    second_sink = JsonlAuditSink(path)
    transition(record, ActionState.DENIED, "second", now=fixed_now, sink=second_sink)

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["reason"] == "first"
    assert json.loads(lines[1])["reason"] == "second"


# ---------------------------------------------------------------------------
# T15: redaction
# ---------------------------------------------------------------------------


def test_redact_args_replaces_secret_looking_keys_and_keeps_benign_ones() -> None:
    args = {
        "api_key": "sk-abc123",
        "access_token": "tok-xyz",
        "Authorization": "Bearer abc",
        "session_cookie": "s3ss10n",
        "creds": {"password": "hunter2", "username": "alice"},
        "extras": [{"secret": "s3cr3t"}, {"location": "kitchen"}],
        "location": "kitchen",
        "target_temperature_c": 22,
        "brightness_pct": 80,
    }
    redacted = redact_args(args)

    assert redacted["api_key"] == REDACTED
    assert redacted["access_token"] == REDACTED
    assert redacted["Authorization"] == REDACTED
    assert redacted["session_cookie"] == REDACTED
    assert redacted["creds"]["password"] == REDACTED
    assert redacted["creds"]["username"] == "alice"
    assert redacted["extras"][0]["secret"] == REDACTED
    assert redacted["extras"][1]["location"] == "kitchen"
    assert redacted["location"] == "kitchen"
    assert redacted["target_temperature_c"] == 22
    assert redacted["brightness_pct"] == 80


def test_redact_args_does_not_mutate_input() -> None:
    args = {"password": "hunter2", "location": "kitchen"}
    before = dict(args)
    redact_args(args)
    assert args == before


# ---------------------------------------------------------------------------
# T16: args_digest equals Approval.args_digest for the same args
# ---------------------------------------------------------------------------


def test_args_digest_matches_approval_args_digest(tmp_path: Path, fixed_now: datetime) -> None:
    args = {"location": "kitchen", "target_temperature_c": 22}
    sink = MemoryAuditSink()
    record = new_action_record(now=fixed_now)
    transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, sink=sink, args=args)

    event = sink.events[0]
    assert event.args_digest == sha256_hex(args)

    from dataclasses import dataclass

    @dataclass
    class FakeCandidate:
        server_binding_identity: str
        tool_name: str
        args: dict

    candidate = FakeCandidate(server_binding_identity="server@sha256:abc", tool_name="set_target_temperature", args=args)
    approval = create_approval(
        candidate,
        action_id="action-1",
        policy_version="v1",
        approved_by="operator",
        approved_at=fixed_now,
        expires_at=fixed_now,
        approval_id="approval-1",
    )
    assert event.args_digest == approval.args_digest


def test_args_digest_is_over_unredacted_args_when_a_secret_key_is_present(fixed_now: datetime) -> None:
    """With a secret-looking key in `args`, the audit digest still equals the Approval digest.

    Guards against hashing the redacted copy: with benign-only args redaction is a no-op,
    so only args that redaction actually changes can tell the two digests apart.
    """
    args = {"location": "kitchen", "api_key": "sk-abc123", "nested": {"access_token": "tok-xyz"}}
    sink = MemoryAuditSink()
    record = new_action_record(now=fixed_now)
    transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, sink=sink, args=args)
    event = sink.events[0]

    assert event.args is not None
    assert event.args["api_key"] == REDACTED
    assert event.args["nested"]["access_token"] == REDACTED
    assert sha256_hex(redact_args(args)) != sha256_hex(args)
    assert event.args_digest == sha256_hex(args)

    from dataclasses import dataclass

    @dataclass
    class FakeCandidate:
        server_binding_identity: str
        tool_name: str
        args: dict

    candidate = FakeCandidate(server_binding_identity="server@sha256:abc", tool_name="set_target_temperature", args=args)
    approval = create_approval(
        candidate,
        action_id="action-1",
        policy_version="v1",
        approved_by="operator",
        approved_at=fixed_now,
        expires_at=fixed_now,
        approval_id="approval-1",
    )
    assert event.args_digest == approval.args_digest


# ---------------------------------------------------------------------------
# T17: long string values are truncated; JSON stays single-line
# ---------------------------------------------------------------------------


def test_long_argument_value_is_truncated(tmp_path: Path, fixed_now: datetime) -> None:
    long_value = "x" * (_MAX_VALUE_CHARS + 100)
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    record = new_action_record(now=fixed_now)
    transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, sink=sink, args={"note": long_value})

    lines = sink.path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert len(parsed["args"]["note"]) == _MAX_VALUE_CHARS
    assert parsed["args"]["note"].endswith("...")
    assert "\n" not in lines[0]


# ---------------------------------------------------------------------------
# T18/T19: load_audit_sink
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_load_audit_sink_unset_or_blank_returns_memory_sink(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is None:
        monkeypatch.delenv(AUDIT_PATH_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(AUDIT_PATH_ENV_VAR, value)
    sink = load_audit_sink()
    assert isinstance(sink, MemoryAuditSink)


def test_load_audit_sink_usable_path_returns_jsonl_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(AUDIT_PATH_ENV_VAR, str(path))
    sink = load_audit_sink()
    assert isinstance(sink, JsonlAuditSink)
    assert sink.path == path


def test_load_audit_sink_directory_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUDIT_PATH_ENV_VAR, str(tmp_path))
    with pytest.raises(ValueError, match=AUDIT_PATH_ENV_VAR):
        load_audit_sink()


def test_load_audit_sink_missing_parent_directory_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "does-not-exist" / "audit.jsonl"
    monkeypatch.setenv(AUDIT_PATH_ENV_VAR, str(path))
    with pytest.raises(ValueError, match=AUDIT_PATH_ENV_VAR):
        load_audit_sink()


# ---------------------------------------------------------------------------
# T20: sink=None writes nothing anywhere
# ---------------------------------------------------------------------------


def test_transition_with_no_sink_writes_no_file(tmp_path: Path, fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, sink=None)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# T21: MemoryAuditSink records events in order
# ---------------------------------------------------------------------------


def test_memory_audit_sink_records_events_in_order(fixed_now: datetime) -> None:
    sink = MemoryAuditSink()
    record = new_action_record(now=fixed_now)
    record = transition(record, ActionState.AUTHORIZED, "one", now=fixed_now, sink=sink)
    transition(record, ActionState.EXECUTING, "two", now=fixed_now, sink=sink)

    assert [event.reason for event in sink.events] == ["one", "two"]
