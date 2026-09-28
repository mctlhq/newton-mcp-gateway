from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from newton_mcp.action.approval import Approval, compute_binding, create_approval, verify_approval
from newton_mcp.runtime.config import HttpTransport, ServerConfig

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(minutes=15)


@dataclass
class FakeCandidate:
    server_binding_identity: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    server_identity: str = "home-bridge"


def _server(url: str, name: str = "home-bridge") -> ServerConfig:
    return ServerConfig(name=name, transport=HttpTransport(kind="streamable-http", url=url))


def _candidate(
    *,
    server_url: str = "https://home-bridge.local/mcp",
    tool_name: str = "set_light_state",
    args: dict | None = None,
) -> FakeCandidate:
    server = _server(server_url)
    return FakeCandidate(
        server_binding_identity=server.binding_identity,
        tool_name=tool_name,
        args=args if args is not None else {"location": "kitchen", "on": True},
    )


def _approve(candidate: FakeCandidate, **overrides) -> Approval:
    kwargs = {
        "action_id": "action-1",
        "policy_version": "example.v1",
        "approved_by": "operator@example.com",
        "approved_at": NOW,
        "expires_at": EXPIRES,
        "approval_id": "approval-1",
    }
    kwargs.update(overrides)
    return create_approval(candidate, **kwargs)


# ---------------------------------------------------------------------------
# T27: fresh approval verifies valid
# ---------------------------------------------------------------------------


def test_fresh_approval_verifies_valid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    check = verify_approval(approval, candidate, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is True


# ---------------------------------------------------------------------------
# T28-T32: one test per bound field
# ---------------------------------------------------------------------------


def test_server_identity_mismatch_is_invalid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    other_candidate = _candidate(server_url="https://other-host.local/mcp")
    check = verify_approval(approval, other_candidate, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False
    assert "server_identity" in check.reason


def test_tool_name_mismatch_is_invalid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    other_candidate = _candidate(tool_name="set_target_temperature")
    check = verify_approval(approval, other_candidate, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


def test_changed_argument_value_is_invalid() -> None:
    candidate = _candidate(args={"location": "kitchen", "on": True})
    approval = _approve(candidate)
    changed = _candidate(args={"location": "kitchen", "on": False})
    check = verify_approval(approval, changed, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


def test_extra_argument_key_is_invalid() -> None:
    candidate = _candidate(args={"location": "kitchen"})
    approval = _approve(candidate)
    changed = _candidate(args={"location": "kitchen", "extra": "value"})
    check = verify_approval(approval, changed, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


def test_removed_argument_key_is_invalid() -> None:
    candidate = _candidate(args={"location": "kitchen", "on": True})
    approval = _approve(candidate)
    changed = _candidate(args={"location": "kitchen"})
    check = verify_approval(approval, changed, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


def test_action_id_mismatch_is_invalid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    check = verify_approval(approval, candidate, "action-2", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


def test_policy_version_mismatch_is_invalid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    check = verify_approval(approval, candidate, "action-1", "other.v2", NOW + timedelta(minutes=1))
    assert check.valid is False


# ---------------------------------------------------------------------------
# T33: mutated expires_at, still future, is invalid
# ---------------------------------------------------------------------------


def test_mutated_expires_at_to_a_still_future_timestamp_is_invalid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    mutated = approval.model_copy(update={"expires_at": EXPIRES + timedelta(minutes=30)})
    check = verify_approval(mutated, candidate, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


# ---------------------------------------------------------------------------
# T34: expiry boundary
# ---------------------------------------------------------------------------


def test_expiry_boundary() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    assert verify_approval(approval, candidate, "action-1", "example.v1", EXPIRES).valid is False
    assert verify_approval(approval, candidate, "action-1", "example.v1", EXPIRES + timedelta(seconds=1)).valid is False
    assert verify_approval(approval, candidate, "action-1", "example.v1", EXPIRES - timedelta(seconds=1)).valid is True


# ---------------------------------------------------------------------------
# T35: naive now raises
# ---------------------------------------------------------------------------


def test_naive_now_raises() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    with pytest.raises(ValueError):
        verify_approval(approval, candidate, "action-1", "example.v1", datetime(2026, 1, 1, 12, 0, 1))


# ---------------------------------------------------------------------------
# T36: canonicalisation -- key insertion order does not affect binding
# ---------------------------------------------------------------------------


def test_args_key_insertion_order_does_not_affect_binding_or_digest() -> None:
    candidate_a = _candidate(args={"location": "kitchen", "on": True})
    candidate_b = _candidate(args={"on": True, "location": "kitchen"})
    approval_a = _approve(candidate_a)
    approval_b = _approve(candidate_b)
    assert approval_a.binding == approval_b.binding
    assert approval_a.args_digest == approval_b.args_digest
    assert verify_approval(approval_a, candidate_b, "action-1", "example.v1", NOW + timedelta(minutes=1)).valid
    assert verify_approval(approval_b, candidate_a, "action-1", "example.v1", NOW + timedelta(minutes=1)).valid


# ---------------------------------------------------------------------------
# T37: unbound fields don't affect validity
# ---------------------------------------------------------------------------


def test_mutating_unbound_fields_leaves_approval_valid() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    mutated = approval.model_copy(
        update={"approved_by": "someone-else@example.com", "approved_at": NOW - timedelta(days=1), "approval_id": "different-id"}
    )
    check = verify_approval(mutated, candidate, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is True


# ---------------------------------------------------------------------------
# T38: no args value leaks into a failure reason
# ---------------------------------------------------------------------------


def test_failure_reason_never_contains_an_argument_value() -> None:
    sentinel = "sentinel-value-should-never-leak-98765"
    candidate = _candidate(args={"message": sentinel})
    approval = _approve(candidate)

    mismatches = [
        _candidate(server_url="https://other-host.local/mcp", args={"message": sentinel}),
        _candidate(tool_name="other_tool", args={"message": sentinel}),
        _candidate(args={"message": "different"}),
    ]
    for other in mismatches:
        check = verify_approval(approval, other, "action-1", "example.v1", NOW + timedelta(minutes=1))
        assert check.valid is False
        assert sentinel not in check.reason

    expired = verify_approval(approval, candidate, "action-1", "example.v1", EXPIRES + timedelta(seconds=1))
    assert sentinel not in expired.reason

    wrong_action = verify_approval(approval, candidate, "wrong-action", "example.v1", NOW + timedelta(minutes=1))
    assert sentinel not in wrong_action.reason

    wrong_policy = verify_approval(approval, candidate, "action-1", "wrong-policy", NOW + timedelta(minutes=1))
    assert sentinel not in wrong_policy.reason


# ---------------------------------------------------------------------------
# T9: re-pointing invalidates an outstanding approval
# ---------------------------------------------------------------------------


def test_repointing_server_url_under_same_name_invalidates_approval() -> None:
    original_candidate = _candidate(server_url="https://home-bridge.local/mcp")
    approval = _approve(original_candidate)

    repointed_candidate = _candidate(server_url="https://attacker-controlled.example/mcp")
    check = verify_approval(approval, repointed_candidate, "action-1", "example.v1", NOW + timedelta(minutes=1))
    assert check.valid is False


# ---------------------------------------------------------------------------
# Approval / compute_binding shape
# ---------------------------------------------------------------------------


def test_approval_is_frozen_and_forbids_unknown_keys() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    with pytest.raises(ValidationError):
        Approval.model_validate({**approval.model_dump(), "extra_field": "nope"})


def test_compute_binding_matches_create_approval() -> None:
    candidate = _candidate()
    approval = _approve(candidate)
    expected = compute_binding(
        server_identity=candidate.server_binding_identity,
        tool_name=candidate.tool_name,
        args=candidate.args,
        action_id="action-1",
        policy_version="example.v1",
        expires_at=EXPIRES,
    )
    assert approval.binding == expected


def test_compute_binding_requires_aware_expires_at() -> None:
    with pytest.raises(ValueError):
        compute_binding(
            server_identity="x",
            tool_name="y",
            args={},
            action_id="a",
            policy_version="v1",
            expires_at=datetime(2026, 1, 1),
        )
