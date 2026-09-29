from __future__ import annotations

from datetime import datetime, timedelta, timezone

import anyio
import pytest

from newton_mcp.action.approval import create_approval
from newton_mcp.runtime.audit import JsonlAuditSink, MemoryAuditSink
from newton_mcp.runtime.catalog import CapabilityCatalog
from newton_mcp.runtime.config import RuntimeConfig, ServerConfig, StdioTransport
from newton_mcp.runtime.executor import ApprovalRejected, Executor, ExecutorError
from newton_mcp.runtime.lifecycle import ActionState, new_action_record, transition
from newton_mcp.runtime.resolver import CandidateAction

from .conftest import (
    FakeToolSpec,
    build_fake_server,
    in_memory_factory,
    raising_factory,
    recording_handler,
    task_group_raising_factory,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _server(name: str = "hvac", command: str = "hvac-cmd") -> ServerConfig:
    return ServerConfig(name=name, transport=StdioTransport(kind="stdio", command=command))


def _candidate(
    server: ServerConfig,
    *,
    tool_name: str = "set_target_temperature",
    args: dict | None = None,
    idempotent: bool = False,
) -> CandidateAction:
    return CandidateAction(
        server_identity=server.resolved_identity,
        server_binding_identity=server.binding_identity,
        tool_name=tool_name,
        args=args if args is not None else {"location": "kitchen", "target_temperature_c": 23},
        read_tool=None,
        read_args={},
        idempotent=idempotent,
        score=1.0,
        why="test",
    )


def _catalog(*servers: ServerConfig, client_factory=None) -> CapabilityCatalog:
    return CapabilityCatalog(RuntimeConfig(servers=tuple(servers)), client_factory=client_factory)


def _authorized_record():
    record = new_action_record(now=NOW)
    return transition(record, ActionState.AUTHORIZED, "policy decided auto", now=NOW)


def _approval(candidate: CandidateAction, record, *, policy_version="policy.v1", expires_at=None):
    return create_approval(
        candidate,
        action_id=record.action_id,
        policy_version=policy_version,
        approved_by="operator@example.com",
        approved_at=NOW,
        expires_at=expires_at or (NOW + timedelta(minutes=15)),
        approval_id="approval-1",
    )


# ---------------------------------------------------------------------------
# T7: happy path
# ---------------------------------------------------------------------------


async def test_happy_call_transitions_authorized_to_executing_to_executed() -> None:
    server = _server()
    call_log: list[tuple[str, dict]] = []
    fake = build_fake_server(
        "hvac", [FakeToolSpec("set_target_temperature", recording_handler(call_log, "set_target_temperature", ["location", "target_temperature_c"]))]
    )
    catalog = _catalog(server)
    sink = MemoryAuditSink()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink)

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record)

    record, outcome = await executor.execute(
        candidate, record, approval=approval, policy_version="policy.v1", now=NOW
    )

    assert outcome.state is ActionState.EXECUTED
    assert record.state is ActionState.EXECUTED
    assert call_log == [("set_target_temperature", {"location": "kitchen", "target_temperature_c": 23})]

    assert [e.to_state for e in sink.events] == ["executing", "executed"]
    for event in sink.events:
        assert event.observation_id and event.action_id and event.tool_call_id and event.verification_id


# ---------------------------------------------------------------------------
# T8: hang -> UNKNOWN; transport failure -> UNKNOWN; MCP error result -> EXECUTED
# ---------------------------------------------------------------------------


async def test_forced_hang_under_short_timeout_ends_unknown() -> None:
    server = _server()

    async def hang() -> dict:
        await anyio.sleep_forever()
        return {}  # pragma: no cover - unreachable

    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", hang)])
    catalog = _catalog(server)
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), call_timeout_seconds=0.2)

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record)

    record, outcome = await executor.execute(
        candidate, record, approval=approval, policy_version="policy.v1", now=NOW
    )

    assert outcome.state is ActionState.UNKNOWN
    assert record.state is ActionState.UNKNOWN


async def test_transport_failure_ends_unknown() -> None:
    server = _server()
    catalog = _catalog(server)
    executor = Executor(catalog, client_factory=raising_factory(RuntimeError("connection refused")))

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record)

    record, outcome = await executor.execute(
        candidate, record, approval=approval, policy_version="policy.v1", now=NOW
    )

    assert outcome.state is ActionState.UNKNOWN
    assert record.state is ActionState.UNKNOWN


async def test_mcp_error_result_still_ends_executed_not_failed() -> None:
    server = _server()

    async def boom() -> dict:
        raise RuntimeError("tool-side failure")

    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", boom)])
    catalog = _catalog(server)
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}))

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record)

    record, outcome = await executor.execute(
        candidate, record, approval=approval, policy_version="policy.v1", now=NOW
    )

    assert outcome.state is ActionState.EXECUTED
    assert record.state is ActionState.EXECUTED
    assert outcome.result.is_error is True


# ---------------------------------------------------------------------------
# T9: unknown server / re-pointed binding_identity -> ExecutorError, zero calls
# ---------------------------------------------------------------------------


async def test_unknown_server_identity_raises_with_zero_calls() -> None:
    server = _server()
    call_log: list[tuple[str, dict]] = []
    fake = build_fake_server(
        "hvac", [FakeToolSpec("set_target_temperature", recording_handler(call_log, "set_target_temperature", ["location", "target_temperature_c"]))]
    )
    # Catalog does not know about "hvac" at all.
    catalog = _catalog()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}))

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record)

    with pytest.raises(ExecutorError):
        await executor.execute(candidate, record, approval=approval, policy_version="policy.v1", now=NOW)

    assert call_log == []


async def test_repointed_binding_identity_raises_with_zero_calls() -> None:
    original_server = _server(command="hvac-cmd")
    call_log: list[tuple[str, dict]] = []
    fake = build_fake_server(
        "hvac", [FakeToolSpec("set_target_temperature", recording_handler(call_log, "set_target_temperature", ["location", "target_temperature_c"]))]
    )

    record = _authorized_record()
    candidate = _candidate(original_server)
    approval = _approval(candidate, record)

    # The catalog now loads a server re-pointed to a different command under the same name.
    repointed_server = _server(command="attacker-cmd")
    catalog = _catalog(repointed_server)
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}))

    with pytest.raises(ExecutorError):
        await executor.execute(candidate, record, approval=approval, policy_version="policy.v1", now=NOW)

    assert call_log == []


# ---------------------------------------------------------------------------
# T18 (owner amendment): approval re-check on every attempt
# ---------------------------------------------------------------------------


async def test_expired_approval_raises_approval_rejected_with_zero_calls_and_record_unchanged() -> None:
    server = _server()
    call_log: list[tuple[str, dict]] = []
    fake = build_fake_server(
        "hvac", [FakeToolSpec("set_target_temperature", recording_handler(call_log, "set_target_temperature", ["location", "target_temperature_c"]))]
    )
    catalog = _catalog(server)
    sink = MemoryAuditSink()
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}), sink=sink)

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record, expires_at=NOW - timedelta(seconds=1))

    with pytest.raises(ApprovalRejected):
        await executor.execute(candidate, record, approval=approval, policy_version="policy.v1", now=NOW)

    assert call_log == []
    assert sink.events == []
    assert record.state is ActionState.AUTHORIZED


async def test_approval_for_another_action_id_raises_approval_rejected() -> None:
    server = _server()
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", lambda: {})])
    catalog = _catalog(server)
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}))

    record = _authorized_record()
    candidate = _candidate(server)
    other_record = new_action_record(now=NOW)
    approval = _approval(candidate, other_record)

    with pytest.raises(ApprovalRejected):
        await executor.execute(candidate, record, approval=approval, policy_version="policy.v1", now=NOW)


async def test_approval_for_another_policy_version_raises_approval_rejected() -> None:
    server = _server()
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", lambda: {})])
    catalog = _catalog(server)
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}))

    record = _authorized_record()
    candidate = _candidate(server)
    approval = _approval(candidate, record, policy_version="policy.v1")

    with pytest.raises(ApprovalRejected):
        await executor.execute(candidate, record, approval=approval, policy_version="policy.v2", now=NOW)


async def test_approval_bound_to_prerepoint_binding_identity_raises_approval_rejected() -> None:
    original_server = _server(command="hvac-cmd")
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", lambda: {})])

    record = _authorized_record()
    original_candidate = _candidate(original_server)
    approval = _approval(original_candidate, record)

    repointed_server = _server(command="repointed-cmd")
    catalog = _catalog(repointed_server)
    executor = Executor(catalog, client_factory=in_memory_factory({"hvac": fake}))
    repointed_candidate = _candidate(repointed_server)

    with pytest.raises(ApprovalRejected):
        await executor.execute(
            repointed_candidate, record, approval=approval, policy_version="policy.v1", now=NOW
        )


# ---------------------------------------------------------------------------
# Owner review of #8: a transport error message never reaches the audit trail
# ---------------------------------------------------------------------------

SENTINEL = "SENTINEL-s3cr3t-9f2c"
_LEAKY_MESSAGE = f"connect failed for https://operator:{SENTINEL}@hvac.example/mcp"


@pytest.mark.parametrize(
    "factory",
    [raising_factory(RuntimeError(_LEAKY_MESSAGE)), task_group_raising_factory(_LEAKY_MESSAGE)],
    ids=["plain-exception", "exception-group"],
)
async def test_transport_error_message_is_not_persisted(tmp_path, factory) -> None:
    server = _server()
    memory = MemoryAuditSink()
    executor = Executor(_catalog(server), client_factory=factory, sink=memory)
    record = _authorized_record()
    candidate = _candidate(server)

    record, outcome = await executor.execute(
        candidate, record, approval=_approval(candidate, record), policy_version="policy.v1", now=NOW
    )

    assert outcome.state is ActionState.UNKNOWN
    assert SENTINEL not in outcome.detail
    assert outcome.detail.startswith("transport failure (")
    for event in memory.events:
        assert SENTINEL not in event.model_dump_json()

    audit_path = tmp_path / "audit.jsonl"
    jsonl_executor = Executor(_catalog(server), client_factory=factory, sink=JsonlAuditSink(audit_path))
    fresh = _authorized_record()
    await jsonl_executor.execute(
        candidate, fresh, approval=_approval(candidate, fresh), policy_version="policy.v1", now=NOW
    )
    assert SENTINEL not in audit_path.read_text()
