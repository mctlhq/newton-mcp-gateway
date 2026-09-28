from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from itertools import product

import pytest

from newton_mcp.runtime.audit import MemoryAuditSink
from newton_mcp.runtime.lifecycle import (
    ALLOWED_TRANSITIONS,
    REQUIRES_VERIFIED_FAILURE,
    ActionRecord,
    ActionState,
    IllegalTransition,
    new_action_record,
    transition,
)


def _record(state: ActionState, *, attempt: int = 0, fixed_now: datetime) -> ActionRecord:
    """An `ActionRecord` in `state`, built by constructing then force-updating.

    `model_copy` bypasses `transition()`'s legality checks entirely, which is
    exactly what a table-driven test over every `(from, to)` pair needs: it
    must be able to construct a record in any state, including one no real
    transition sequence would reach.
    """
    base = new_action_record(now=fixed_now, id_factory=lambda: "deadbeefdeadbeef")
    return base.model_copy(update={"state": state, "attempt": attempt})


# ---------------------------------------------------------------------------
# T1: exhaustive table-driven transition test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("from_state,to_state", list(product(ActionState, ActionState)))
def test_transition_table_is_exhaustive(from_state: ActionState, to_state: ActionState, fixed_now: datetime) -> None:
    record = _record(from_state, fixed_now=fixed_now)
    is_retry = from_state is ActionState.FAILED and to_state is ActionState.EXECUTING

    if to_state in ALLOWED_TRANSITIONS[from_state]:
        result = transition(
            record,
            to_state,
            "test transition",
            now=fixed_now,
            verified_failure=is_retry,
        )
        assert result.state is to_state
    else:
        with pytest.raises(IllegalTransition):
            transition(record, to_state, "test transition", now=fixed_now, verified_failure=is_retry)


def test_only_verifying_can_reach_failed() -> None:
    for state, targets in ALLOWED_TRANSITIONS.items():
        if state is ActionState.VERIFYING:
            continue
        assert ActionState.FAILED not in targets


# ---------------------------------------------------------------------------
# T2/T3: the two named safety edges
# ---------------------------------------------------------------------------


def test_unknown_cannot_go_straight_back_to_executing(fixed_now: datetime) -> None:
    record = _record(ActionState.UNKNOWN, fixed_now=fixed_now)
    with pytest.raises(IllegalTransition) as excinfo:
        transition(record, ActionState.EXECUTING, "retry", now=fixed_now)
    assert excinfo.value.allowed == {ActionState.VERIFYING, ActionState.ESCALATED}


def test_proposed_cannot_skip_authorization(fixed_now: datetime) -> None:
    record = _record(ActionState.PROPOSED, fixed_now=fixed_now)
    with pytest.raises(IllegalTransition):
        transition(record, ActionState.EXECUTING, "skip", now=fixed_now)


# ---------------------------------------------------------------------------
# T4: terminal states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_state", [ActionState.DENIED, ActionState.SUCCEEDED, ActionState.ESCALATED])
def test_terminal_states_allow_no_outgoing_transition(terminal_state: ActionState, fixed_now: datetime) -> None:
    record = _record(terminal_state, fixed_now=fixed_now)
    for target in ActionState:
        with pytest.raises(IllegalTransition, match="terminal"):
            transition(record, target, "attempt", now=fixed_now)


# ---------------------------------------------------------------------------
# T5: ALLOWED_TRANSITIONS covers every ActionState exactly once
# ---------------------------------------------------------------------------


def test_allowed_transitions_covers_every_state_exactly_once() -> None:
    assert set(ALLOWED_TRANSITIONS.keys()) == set(ActionState)
    for targets in ALLOWED_TRANSITIONS.values():
        for target in targets:
            assert isinstance(target, ActionState)


# ---------------------------------------------------------------------------
# T6: verified_failure guard
# ---------------------------------------------------------------------------


def test_failed_to_executing_requires_verified_failure(fixed_now: datetime) -> None:
    record = _record(ActionState.FAILED, fixed_now=fixed_now)
    with pytest.raises(IllegalTransition):
        transition(record, ActionState.EXECUTING, "retry", now=fixed_now)


def test_failed_to_executing_succeeds_with_verified_failure_and_records_it_on_audit(fixed_now: datetime) -> None:
    record = _record(ActionState.FAILED, attempt=1, fixed_now=fixed_now)
    sink = MemoryAuditSink()
    result = transition(
        record, ActionState.EXECUTING, "retrying after verified failure", now=fixed_now,
        verified_failure=True, sink=sink,
    )
    assert result.state is ActionState.EXECUTING
    assert sink.events[0].verified_failure is True


# ---------------------------------------------------------------------------
# T7: a rejected transition leaves the input record untouched and audits nothing
# ---------------------------------------------------------------------------


def test_rejected_transition_leaves_record_unchanged_and_writes_nothing(fixed_now: datetime) -> None:
    record = _record(ActionState.PROPOSED, fixed_now=fixed_now)
    before = record.model_dump()
    sink = MemoryAuditSink()
    with pytest.raises(IllegalTransition):
        transition(record, ActionState.EXECUTING, "nope", now=fixed_now, sink=sink)
    assert record.model_dump() == before
    assert sink.events == []


# ---------------------------------------------------------------------------
# T8: id generation
# ---------------------------------------------------------------------------


def test_new_action_record_generates_four_prefixed_ids(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    assert record.observation_id.startswith("obs-")
    assert record.action_id.startswith("act-")
    assert record.tool_call_id.startswith("call-")
    assert record.verification_id.startswith("ver-")


def test_new_action_record_deterministic_ids(fixed_now: datetime, deterministic_id_factory: Callable[[], str]) -> None:
    record = new_action_record(now=fixed_now, id_factory=deterministic_id_factory)
    assert record.observation_id == "obs-0000000000000001"
    assert record.action_id == "act-0000000000000002"
    assert record.tool_call_id == "call-0000000000000003"
    assert record.verification_id == "ver-0000000000000004"


def test_new_action_record_preserves_supplied_ids(fixed_now: datetime) -> None:
    record = new_action_record(
        now=fixed_now,
        observation_id="obs-explicit",
        action_id="act-explicit",
        tool_call_id="call-explicit",
        verification_id="ver-explicit",
    )
    assert record.observation_id == "obs-explicit"
    assert record.action_id == "act-explicit"
    assert record.tool_call_id == "call-explicit"
    assert record.verification_id == "ver-explicit"


def test_new_action_record_initial_state(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    assert record.state is ActionState.PROPOSED
    assert record.attempt == 0
    assert record.created_at == fixed_now
    assert record.updated_at == fixed_now


def test_new_action_record_naive_now_raises() -> None:
    with pytest.raises(ValueError):
        new_action_record(now=datetime(2026, 1, 1, 12, 0, 0))


# ---------------------------------------------------------------------------
# T9: attempt counter
# ---------------------------------------------------------------------------


def test_attempt_counter_increments_only_on_executing(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    record = transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now)
    assert record.attempt == 0
    record = transition(record, ActionState.EXECUTING, "calling tool", now=fixed_now)
    assert record.attempt == 1
    record = transition(record, ActionState.EXECUTED, "tool responded", now=fixed_now)
    assert record.attempt == 1
    record = transition(record, ActionState.VERIFYING, "checking outcome", now=fixed_now)
    assert record.attempt == 1
    record = transition(record, ActionState.FAILED, "outcome verified failed", now=fixed_now)
    assert record.attempt == 1
    record = transition(record, ActionState.EXECUTING, "retry", now=fixed_now, verified_failure=True)
    assert record.attempt == 2


# ---------------------------------------------------------------------------
# T10: attempt-scoped ids (owner amendment)
# ---------------------------------------------------------------------------


def test_attempt_scoped_ids(fixed_now: datetime, deterministic_id_factory: Callable[[], str]) -> None:
    record = new_action_record(now=fixed_now, id_factory=deterministic_id_factory)
    creation_call_id = record.tool_call_id
    creation_verification_id = record.verification_id

    record = transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now)
    record = transition(record, ActionState.EXECUTING, "calling tool", now=fixed_now)
    assert record.tool_call_id == creation_call_id
    assert record.verification_id == creation_verification_id

    record = transition(record, ActionState.EXECUTED, "tool responded", now=fixed_now)
    record = transition(record, ActionState.VERIFYING, "checking outcome", now=fixed_now)
    assert record.tool_call_id == creation_call_id
    assert record.verification_id == creation_verification_id

    # Take an alternate path through UNKNOWN -> VERIFYING and confirm the same holds.
    unknown_record = new_action_record(now=fixed_now, id_factory=deterministic_id_factory)
    unknown_creation_call_id = unknown_record.tool_call_id
    unknown_creation_verification_id = unknown_record.verification_id
    unknown_record = transition(unknown_record, ActionState.AUTHORIZED, "approved", now=fixed_now)
    unknown_record = transition(unknown_record, ActionState.EXECUTING, "calling tool", now=fixed_now)
    unknown_record = transition(unknown_record, ActionState.UNKNOWN, "timeout", now=fixed_now)
    unknown_record = transition(unknown_record, ActionState.VERIFYING, "checking outcome", now=fixed_now)
    assert unknown_record.tool_call_id == unknown_creation_call_id
    assert unknown_record.verification_id == unknown_creation_verification_id

    record = transition(record, ActionState.FAILED, "verified failed", now=fixed_now)
    record = transition(record, ActionState.EXECUTING, "retry", now=fixed_now, verified_failure=True)
    assert record.tool_call_id != creation_call_id
    assert record.verification_id != creation_verification_id
    assert record.tool_call_id != record.verification_id
    assert record.attempt == 2


def test_retry_supplied_id_wins_and_unsupplied_id_is_still_generated(
    fixed_now: datetime, deterministic_id_factory: Callable[[], str]
) -> None:
    record = new_action_record(now=fixed_now, id_factory=deterministic_id_factory)
    record = transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now)
    record = transition(record, ActionState.EXECUTING, "calling tool", now=fixed_now)
    record = transition(record, ActionState.EXECUTED, "tool responded", now=fixed_now)
    record = transition(record, ActionState.VERIFYING, "checking outcome", now=fixed_now)
    record = transition(record, ActionState.FAILED, "verified failed", now=fixed_now)

    record = transition(
        record,
        ActionState.EXECUTING,
        "retry",
        now=fixed_now,
        verified_failure=True,
        tool_call_id="call-explicit-retry",
        id_factory=deterministic_id_factory,
    )
    assert record.tool_call_id == "call-explicit-retry"
    assert record.verification_id.startswith("ver-")


# ---------------------------------------------------------------------------
# T11: id-keyword validation
# ---------------------------------------------------------------------------


def test_observation_id_keyword_raises(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, observation_id="obs-nope")


def test_action_id_keyword_raises(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, action_id="act-nope")


def test_tool_call_id_on_authorized_to_executing_raises(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    record = transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.EXECUTING, "calling tool", now=fixed_now, tool_call_id="call-nope")


def test_verification_id_on_executed_to_verifying_raises(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    record = transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now)
    record = transition(record, ActionState.EXECUTING, "calling tool", now=fixed_now)
    record = transition(record, ActionState.EXECUTED, "tool responded", now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.VERIFYING, "checking outcome", now=fixed_now, verification_id="ver-nope")


def test_unknown_id_keyword_raises(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.AUTHORIZED, "approved", now=fixed_now, bogus_id="x")


# ---------------------------------------------------------------------------
# T12: blank reason / naive now
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank_reason", ["", "   ", "\t"])
def test_blank_reason_raises(blank_reason: str, fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.AUTHORIZED, blank_reason, now=fixed_now)


def test_naive_now_raises(fixed_now: datetime) -> None:
    record = new_action_record(now=fixed_now)
    with pytest.raises(ValueError):
        transition(record, ActionState.AUTHORIZED, "approved", now=datetime(2026, 1, 1, 12, 0, 1))
