from __future__ import annotations

from datetime import datetime, timedelta, timezone

import anyio
import pytest

from newton_mcp.action.approval import create_approval
from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
from newton_mcp.runtime.audit import MemoryAuditSink
from newton_mcp.runtime.catalog import CapabilityCatalog
from newton_mcp.runtime.config import CapabilityConfig, RuntimeConfig, ServerConfig, StdioTransport, TargetMatch
from newton_mcp.runtime.executor import Executor, run_action
from newton_mcp.runtime.lifecycle import ActionState, new_action_record, transition
from newton_mcp.runtime.resolver import CandidateAction
from newton_mcp.runtime.verifier import Verifier

from .conftest import DeterministicClock, FakeToolSpec, build_fake_server, in_memory_factory, scripted_handler

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class SequentialClock:
    """Returns each of `values` in order, then keeps returning the last one."""

    def __init__(self, values: list[datetime]) -> None:
        self._values = list(values)

    def __call__(self) -> datetime:
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def _server() -> ServerConfig:
    return ServerConfig(name="hvac", transport=StdioTransport(kind="stdio", command="hvac-cmd"))


def _capability(*, idempotent: bool) -> CapabilityConfig:
    return CapabilityConfig(
        server="hvac",
        tool="set_target_temperature",
        goal_prefixes=("reduce_room_temperature",),
        target=TargetMatch(type="environment"),
        arguments={},
        read_tool="get_room_temperature",
        read_arguments={},
        idempotent=idempotent,
    )


async def _build(action_handler, read_handler, *, idempotent: bool):
    capability = _capability(idempotent=idempotent)
    fake = build_fake_server(
        "hvac",
        [
            FakeToolSpec("set_target_temperature", action_handler),
            FakeToolSpec("get_room_temperature", read_handler),
        ],
    )
    config = RuntimeConfig(servers=(_server(),), capabilities=(capability,))
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))
    await catalog.refresh()
    return catalog, fake


def _candidate(*, idempotent: bool) -> CandidateAction:
    server = _server()
    return CandidateAction(
        server_identity=server.resolved_identity,
        server_binding_identity=server.binding_identity,
        tool_name="set_target_temperature",
        args={},
        read_tool="get_room_temperature",
        read_args={},
        idempotent=idempotent,
        score=1.0,
        why="test",
    )


def _contract(*, retry_limit: int = 0, timeout_seconds: int = 1) -> PhysicalActionContract:
    return PhysicalActionContract(
        goal="reduce_room_temperature",
        reason="test",
        target=Target(type="environment", location="kitchen"),
        risk=Risk.LOW,
        verification=Verification(
            condition={"path": "temperature_c", "op": "le", "value": 24},
            timeout_seconds=timeout_seconds,
            retry_limit=retry_limit,
        ),
    )


def _authorized_record():
    record = new_action_record(now=NOW)
    return transition(record, ActionState.AUTHORIZED, "policy decided auto", now=NOW)


def _approval(candidate, record, *, expires_at=None):
    return create_approval(
        candidate,
        action_id=record.action_id,
        policy_version="policy.v1",
        approved_by="operator@example.com",
        approved_at=NOW,
        expires_at=expires_at or (NOW + timedelta(hours=1)),
        approval_id="approval-1",
    )


def _action_call_counter(log: list[str]):
    async def handler() -> dict:
        log.append("call")
        return {}

    return handler


# ---------------------------------------------------------------------------
# T12: call succeeds, verified outcome never satisfied
# ---------------------------------------------------------------------------


async def test_non_idempotent_verified_failure_escalates_with_exactly_one_call() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)  # never satisfies <= 24
    catalog, fake = await _build(_action_call_counter(action_log), read_handler, idempotent=False)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink)
    # poll_interval > timeout so exactly one poll happens per verify() call.
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=False)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=5)  # irrelevant: not idempotent, never retries

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED
    assert record.state is ActionState.ESCALATED
    assert action_log == ["call"]


async def test_idempotent_with_retry_limit_2_makes_exactly_three_calls_then_escalates() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)
    catalog, fake = await _build(_action_call_counter(action_log), read_handler, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink)
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=True)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=2)

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED
    assert action_log == ["call", "call", "call"]


# ---------------------------------------------------------------------------
# T13: forced timeout (UNKNOWN), verification shows outcome already met -> SUCCEEDED, one call
# ---------------------------------------------------------------------------


async def test_forced_timeout_but_outcome_already_met_succeeds_with_exactly_one_call() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 20}])  # already satisfies <= 24

    async def hang_and_log() -> dict:
        action_log.append("call")
        await anyio.sleep_forever()
        return {}  # pragma: no cover

    catalog, fake = await _build(hang_and_log, read_handler, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink, call_timeout_seconds=0.2
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=True)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=3)

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.SUCCEEDED
    assert action_log == ["call"]


# ---------------------------------------------------------------------------
# T14: forced timeout, outcome not met, idempotent=False -> ESCALATED, one call
# ---------------------------------------------------------------------------


async def test_forced_timeout_outcome_not_met_non_idempotent_escalates_with_one_call() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)

    async def hang_and_log() -> dict:
        action_log.append("call")
        await anyio.sleep_forever()
        return {}  # pragma: no cover

    catalog, fake = await _build(hang_and_log, read_handler, idempotent=False)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink, call_timeout_seconds=0.2
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=False)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=5)

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED
    assert action_log == ["call"]


# ---------------------------------------------------------------------------
# T15: forced timeout, outcome not met, idempotent=True -> exactly retry_limit+1 calls
# ---------------------------------------------------------------------------


async def test_forced_timeout_outcome_not_met_idempotent_retry_limit_1_makes_two_calls() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)

    async def hang_and_log() -> dict:
        action_log.append("call")
        await anyio.sleep_forever()
        return {}  # pragma: no cover

    catalog, fake = await _build(hang_and_log, read_handler, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink, call_timeout_seconds=0.2
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=True)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=1)

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED
    assert action_log == ["call", "call"]


async def test_forced_timeout_outcome_not_met_idempotent_retry_limit_2_honours_configured_value() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)

    async def hang_and_log() -> dict:
        action_log.append("call")
        await anyio.sleep_forever()
        return {}  # pragma: no cover

    catalog, fake = await _build(hang_and_log, read_handler, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink, call_timeout_seconds=0.2
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=True)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=2)

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED
    assert action_log == ["call", "call", "call"]


# ---------------------------------------------------------------------------
# T16: full audited transition sequence for a retry scenario
# ---------------------------------------------------------------------------


async def test_retry_scenario_full_audit_sequence() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)

    async def hang_and_log() -> dict:
        action_log.append("call")
        await anyio.sleep_forever()
        return {}  # pragma: no cover

    catalog, fake = await _build(hang_and_log, read_handler, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink, call_timeout_seconds=0.2
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=True)
    approval = _approval(candidate, record)
    contract = _contract(retry_limit=1)

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED

    transitions = [(e.from_state, e.to_state, e.attempt, e.verified_failure) for e in sink.events]
    assert transitions == [
        ("authorized", "executing", 1, False),
        ("executing", "unknown", 1, False),
        ("unknown", "verifying", 1, False),
        ("verifying", "failed", 1, False),
        ("failed", "executing", 2, True),
        ("executing", "unknown", 2, False),
        ("unknown", "verifying", 2, False),
        ("verifying", "failed", 2, False),
        ("failed", "escalated", 2, False),
    ]

    # Exactly one EXECUTING line per attempt; run_action() itself never transitions into EXECUTING.
    executing_events = [e for e in sink.events if e.to_state == "executing"]
    assert len(executing_events) == 2

    # Every line carries all four non-null correlation ids.
    for event in sink.events:
        assert event.observation_id and event.action_id and event.tool_call_id and event.verification_id

    # observation_id/action_id are fixed; tool_call_id/verification_id differ across the retry.
    obs_ids = {e.observation_id for e in sink.events}
    action_ids = {e.action_id for e in sink.events}
    assert len(obs_ids) == 1
    assert len(action_ids) == 1

    attempt_1_ids = {(e.tool_call_id, e.verification_id) for e in sink.events if e.attempt == 1}
    attempt_2_ids = {(e.tool_call_id, e.verification_id) for e in sink.events if e.attempt == 2}
    assert len(attempt_1_ids) == 1
    assert len(attempt_2_ids) == 1
    assert attempt_1_ids != attempt_2_ids


# ---------------------------------------------------------------------------
# T19 (owner amendment): approval expires before the retry
# ---------------------------------------------------------------------------


async def test_approval_expires_before_retry_escalates_naming_approval_with_one_call() -> None:
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)

    async def hang_and_log() -> dict:
        action_log.append("call")
        await anyio.sleep_forever()
        return {}  # pragma: no cover

    catalog, fake = await _build(hang_and_log, read_handler, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink, call_timeout_seconds=0.2
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    record = _authorized_record()
    candidate = _candidate(idempotent=True)
    # Approval expires shortly after NOW -- well before the retry's now_fn() value.
    approval = _approval(candidate, record, expires_at=NOW + timedelta(seconds=30))
    contract = _contract(retry_limit=1)

    now_fn = SequentialClock([NOW, NOW, NOW + timedelta(minutes=10), NOW + timedelta(minutes=10)])

    record, final_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=now_fn,
    )

    assert final_state is ActionState.ESCALATED
    assert record.state is ActionState.ESCALATED
    assert action_log == ["call"]

    last_event = sink.events[-1]
    assert last_event.from_state == "failed"
    assert last_event.to_state == "escalated"
    assert "approval" in last_event.reason


# ---------------------------------------------------------------------------
# Owner review of #8: one audit sink for the whole run
# ---------------------------------------------------------------------------


async def test_run_action_refuses_split_audit_sinks_before_any_call() -> None:
    action_log: list[str] = []
    catalog, fake = await _build(_action_call_counter(action_log), scripted_handler([]), idempotent=True)
    clock = DeterministicClock()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=MemoryAuditSink())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=MemoryAuditSink(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    record = _authorized_record()
    candidate = _candidate(idempotent=True)

    with pytest.raises(ValueError, match="share one audit sink"):
        await run_action(
            candidate,
            _contract(),
            record,
            approval=_approval(candidate, record),
            policy_version="policy.v1",
            executor=executor,
            verifier=verifier,
            now_fn=lambda: NOW,
        )
    assert action_log == []


async def test_terminal_escalation_lands_in_the_shared_sink() -> None:
    """The FAILED -> ESCALATED line written by run_action() itself is in the same trail."""
    action_log: list[str] = []
    read_handler = scripted_handler([{"temperature_c": 30}] * 10)
    catalog, fake = await _build(_action_call_counter(action_log), read_handler, idempotent=False)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink)
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        sink=sink,
        poll_interval_seconds=1000,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    record = _authorized_record()
    candidate = _candidate(idempotent=False)

    _record, final_state = await run_action(
        candidate,
        _contract(),
        record,
        approval=_approval(candidate, record),
        policy_version="policy.v1",
        executor=executor,
        verifier=verifier,
        now_fn=lambda: NOW,
    )

    assert final_state is ActionState.ESCALATED
    assert [(e.from_state, e.to_state) for e in sink.events] == [
        ("authorized", "executing"),
        ("executing", "executed"),
        ("executed", "verifying"),
        ("verifying", "failed"),
        ("failed", "escalated"),
    ]


@pytest.mark.parametrize("initial_negative", [False, True])
@pytest.mark.parametrize("lost_observation", ["error", "empty", "wrong-type", "non-finite"])
async def test_lost_observability_after_negative_read_never_retries(lost_observation, initial_negative) -> None:
    action_log: list[str] = []
    reads = 0

    async def read() -> dict:
        nonlocal reads
        reads += 1
        if reads == 1 and initial_negative:
            return {"temperature_c": 30}
        if lost_observation == "error":
            raise RuntimeError("sensor offline")
        if lost_observation == "empty":
            return {}
        if lost_observation == "non-finite":
            return {"temperature_c": float("nan")}
        return {"temperature_c": "unavailable"}

    catalog, fake = await _build(_action_call_counter(action_log), read, idempotent=True)
    sink = MemoryAuditSink()
    clock = DeterministicClock()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink)
    verifier = Verifier(
        catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink,
        poll_interval_seconds=0.25, clock=clock.clock, sleep=clock.sleep,
    )
    candidate = _candidate(idempotent=True)
    record = _authorized_record()
    _, state = await run_action(
        candidate, _contract(retry_limit=1), record, approval=_approval(candidate, record),
        policy_version="policy.v1", executor=executor, verifier=verifier, now_fn=lambda: NOW,
    )
    assert state is ActionState.ESCALATED
    assert action_log == ["call"]
    assert "failed" not in [event.to_state for event in sink.events]


async def test_caller_cannot_change_retry_budget_during_execution() -> None:
    contract = _contract(retry_limit=0)
    calls = []

    async def action() -> dict:
        calls.append("call")
        contract.verification.retry_limit = 5
        return {}

    async def read() -> dict:
        return {"temperature_c": 30}

    catalog, fake = await _build(action, read, idempotent=True)
    clock = DeterministicClock()
    sink = MemoryAuditSink()
    factory = in_memory_factory({"hvac": fake})
    executor = Executor(catalog, client_factory=factory, sink=sink)
    verifier = Verifier(catalog, client_factory=factory, sink=sink, clock=clock.clock, sleep=clock.sleep)
    candidate = _candidate(idempotent=True)
    record = _authorized_record()
    await run_action(
        candidate, contract, record, approval=_approval(candidate, record), policy_version="policy.v1",
        executor=executor, verifier=verifier, now_fn=lambda: NOW,
    )
    assert calls == ["call"]
    assert contract.verification.retry_limit == 5
