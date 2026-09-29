from __future__ import annotations

from datetime import datetime, timezone

from mcp.types import ToolAnnotations

from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
from newton_mcp.runtime.catalog import CapabilityCatalog
from newton_mcp.runtime.config import CapabilityConfig, RuntimeConfig, ServerConfig, StdioTransport, TargetMatch
from newton_mcp.runtime.lifecycle import ActionState, new_action_record, transition
from newton_mcp.runtime.resolver import CandidateAction
from newton_mcp.runtime.verifier import Verifier, observation_from_result

from .conftest import FakeToolSpec, build_fake_server, in_memory_factory, scripted_handler

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _server(name: str = "hvac") -> ServerConfig:
    return ServerConfig(name=name, transport=StdioTransport(kind="stdio", command=f"{name}-cmd"))


def _capability(
    *, read_tool: str | None = "get_room_temperature", tool: str = "set_target_temperature"
) -> CapabilityConfig:
    return CapabilityConfig(
        server="hvac",
        tool=tool,
        goal_prefixes=("reduce_room_temperature",),
        target=TargetMatch(type="environment"),
        arguments={},
        read_tool=read_tool,
        read_arguments={"location": "kitchen"},
    )


async def _action_tool() -> dict:
    return {}


async def _catalog_for(tools, capability: CapabilityConfig) -> CapabilityCatalog:
    """Build a refreshed catalog for `capability`, auto-adding a stub action tool.

    The verifier only cares about the *read* tool, but `CapabilityCatalog.refresh()`
    never creates a `CatalogEntry` for a capability whose own action tool is
    undiscovered (`tool_missing`), so every test here needs the action tool
    present even though it is never called.
    """
    all_tools = list(tools)
    if not any(spec.name == capability.tool for spec in all_tools):
        all_tools = [FakeToolSpec(capability.tool, _action_tool), *all_tools]
    fake = build_fake_server("hvac", all_tools)
    config = RuntimeConfig(servers=(_server(),), capabilities=(capability,))
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))
    await catalog.refresh()
    return catalog


def _candidate(*, read_tool: str | None = "get_room_temperature") -> CandidateAction:
    server = _server()
    return CandidateAction(
        server_identity=server.resolved_identity,
        server_binding_identity=server.binding_identity,
        tool_name="set_target_temperature",
        args={},
        read_tool=read_tool,
        read_args={"location": "kitchen"},
        idempotent=True,
        score=1.0,
        why="test",
    )


def _contract(*, timeout_seconds: int = 100, retry_limit: int = 0) -> PhysicalActionContract:
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


def _executing_record():
    record = new_action_record(now=NOW)
    record = transition(record, ActionState.AUTHORIZED, "policy decided auto", now=NOW)
    record = transition(record, ActionState.EXECUTING, "executor issuing the call attempt", now=NOW)
    return transition(record, ActionState.EXECUTED, "call returned", now=NOW)


# ---------------------------------------------------------------------------
# T10: satisfied first observation / third poll
# ---------------------------------------------------------------------------


async def test_satisfied_on_first_observation_succeeds_after_exactly_one_read(deterministic_clock) -> None:
    read_log: list[str] = []

    async def get_room_temperature() -> dict:
        read_log.append("read")
        return {"temperature_c": 23}

    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", get_room_temperature)], _capability())
    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", get_room_temperature)])
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        poll_interval_seconds=10,
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(), record, now=NOW)

    assert outcome.state is ActionState.SUCCEEDED
    assert outcome.observations == 1
    assert len(read_log) == 1
    assert record.state is ActionState.SUCCEEDED


async def test_satisfied_on_third_poll_succeeds_with_three_reads_and_no_action_call(deterministic_clock) -> None:
    action_log: list[str] = []

    async def set_target_temperature() -> dict:
        action_log.append("call")
        return {}

    read_handler = scripted_handler([{"temperature_c": 30}, {"temperature_c": 27}, {"temperature_c": 23}])
    fake = build_fake_server(
        "hvac",
        [
            FakeToolSpec("set_target_temperature", set_target_temperature),
            FakeToolSpec("get_room_temperature", read_handler),
        ],
    )
    catalog = await _catalog_for(
        [
            FakeToolSpec("set_target_temperature", set_target_temperature),
            FakeToolSpec("get_room_temperature", read_handler),
        ],
        _capability(),
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        poll_interval_seconds=10,
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(), record, now=NOW)

    assert outcome.state is ActionState.SUCCEEDED
    assert outcome.observations == 3
    assert action_log == []


# ---------------------------------------------------------------------------
# T11: never satisfied / zero observations / unverifiable pre-poll
# ---------------------------------------------------------------------------


async def test_never_satisfied_by_deadline_with_observations_is_failed(deterministic_clock) -> None:
    read_handler = scripted_handler([{"temperature_c": 30}, {"temperature_c": 29}, {"temperature_c": 28}])
    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", read_handler)])
    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", read_handler)], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        poll_interval_seconds=10,
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(timeout_seconds=20), record, now=NOW)

    assert outcome.state is ActionState.FAILED
    assert outcome.observations >= 1
    assert record.state is ActionState.FAILED


async def test_zero_obtainable_observations_every_read_errors_is_escalated(deterministic_clock) -> None:
    async def failing_read() -> dict:
        raise RuntimeError("sensor offline")

    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", failing_read)])
    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", failing_read)], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        poll_interval_seconds=10,
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(timeout_seconds=20), record, now=NOW)

    assert outcome.state is ActionState.ESCALATED
    assert outcome.observations == 0
    assert record.state is ActionState.ESCALATED


async def test_no_read_tool_configured_escalates_with_zero_reads(deterministic_clock) -> None:
    fake = build_fake_server("hvac", [])
    catalog = await _catalog_for([], _capability(read_tool=None))
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(read_tool=None), _contract(), record, now=NOW)

    assert outcome.state is ActionState.ESCALATED
    assert outcome.observations == 0
    assert record.state is ActionState.ESCALATED


async def test_read_tool_configured_but_not_discovered_escalates(deterministic_clock) -> None:
    # get_room_temperature is never registered on the fake server, so it's read_tool_missing.
    fake = build_fake_server("hvac", [])
    catalog = await _catalog_for([], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(), record, now=NOW)

    assert outcome.state is ActionState.ESCALATED
    assert outcome.observations == 0


async def test_discovered_read_tool_with_read_only_hint_false_escalates_with_zero_reads(deterministic_clock) -> None:
    read_log: list[str] = []

    async def get_room_temperature() -> dict:
        read_log.append("read")
        return {"temperature_c": 23}

    fake = build_fake_server(
        "hvac",
        [
            FakeToolSpec(
                "get_room_temperature", get_room_temperature, annotations=ToolAnnotations(read_only_hint=False)
            )
        ],
    )
    catalog = await _catalog_for(
        [
            FakeToolSpec(
                "get_room_temperature", get_room_temperature, annotations=ToolAnnotations(read_only_hint=False)
            )
        ],
        _capability(),
    )
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(), record, now=NOW)

    assert outcome.state is ActionState.ESCALATED
    assert outcome.observations == 0
    assert read_log == []


async def test_unannotated_read_only_hint_is_allowed(deterministic_clock) -> None:
    async def get_room_temperature() -> dict:
        return {"temperature_c": 23}

    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", get_room_temperature)])
    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", get_room_temperature)], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record = _executing_record()
    record, outcome = await verifier.verify(_candidate(), _contract(), record, now=NOW)

    assert outcome.state is ActionState.SUCCEEDED


# ---------------------------------------------------------------------------
# observation_from_result()
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, *, is_error=False, structured_content=None, content=None):
        self.is_error = is_error
        self.structured_content = structured_content
        self.content = content or []


class _FakeText:
    def __init__(self, text):
        self.text = text


def test_observation_from_result_error_result_is_never_an_observation() -> None:
    result = _FakeResult(is_error=True, structured_content={"temperature_c": 23})
    assert observation_from_result(result) is None


def test_observation_from_result_prefers_structured_content() -> None:
    result = _FakeResult(structured_content={"temperature_c": 23})
    assert observation_from_result(result) == {"temperature_c": 23}


def test_observation_from_result_falls_back_to_single_json_text_block() -> None:
    result = _FakeResult(content=[_FakeText('{"temperature_c": 23}')])
    assert observation_from_result(result) == {"temperature_c": 23}


def test_observation_from_result_non_json_text_is_not_an_observation() -> None:
    result = _FakeResult(content=[_FakeText("not json")])
    assert observation_from_result(result) is None


def test_observation_from_result_non_object_json_text_is_not_an_observation() -> None:
    result = _FakeResult(content=[_FakeText("[1, 2, 3]")])
    assert observation_from_result(result) is None


def test_observation_from_result_no_content_is_not_an_observation() -> None:
    result = _FakeResult(content=[])
    assert observation_from_result(result) is None


# ---------------------------------------------------------------------------
# Owner review of #8: read_args, poll schedule/deadline, re-pointed server
# ---------------------------------------------------------------------------


async def test_read_tool_receives_exactly_read_args_never_action_args(deterministic_clock) -> None:
    """T21: the read tool gets the rendered `read_args` and never the action's `args`."""
    call_log: list[dict] = []

    # Declares the action's extra parameter as optional, so the in-process server
    # would pass it through (it drops undeclared arguments) if the verifier ever
    # sent the action `args` instead of `read_args`.
    async def read_tool(location: str | None = None, target_temperature_c: int | None = None) -> dict:
        call_log.append({"location": location, "target_temperature_c": target_temperature_c})
        return {"temperature_c": 23}

    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", read_tool)])
    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", read_tool)], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )
    candidate = _candidate().model_copy(update={"args": {"location": "kitchen", "target_temperature_c": 23}})

    _record, outcome = await verifier.verify(candidate, _contract(), _executing_record(), now=NOW)

    assert outcome.state is ActionState.SUCCEEDED
    assert call_log == [{"location": "kitchen", "target_temperature_c": None}]


async def test_polls_start_at_t0_and_none_starts_after_the_deadline(deterministic_clock) -> None:
    """First poll at t=0, one per interval, the last one no later than the deadline."""
    poll_times: list[float] = []

    async def get_room_temperature() -> dict:
        poll_times.append(deterministic_clock.now)
        return {"temperature_c": 30}  # never satisfies <= 24

    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", get_room_temperature)])
    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", get_room_temperature)], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        poll_interval_seconds=5,
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )

    record, outcome = await verifier.verify(
        _candidate(), _contract(timeout_seconds=20), _executing_record(), now=NOW
    )

    assert poll_times == [0, 5, 10, 15, 20]
    assert outcome.state is ActionState.FAILED
    assert outcome.observations == 5
    assert record.state is ActionState.FAILED


async def test_re_pointed_server_is_never_verified_against(deterministic_clock) -> None:
    """A candidate resolved against another binding_identity escalates with zero reads."""
    read_log: list[str] = []

    async def get_room_temperature() -> dict:
        read_log.append("read")
        return {"temperature_c": 23}  # would satisfy the condition

    fake = build_fake_server("hvac", [FakeToolSpec("get_room_temperature", get_room_temperature)])
    catalog = await _catalog_for([FakeToolSpec("get_room_temperature", get_room_temperature)], _capability())
    verifier = Verifier(
        catalog,
        client_factory=in_memory_factory({"hvac": fake}),
        clock=deterministic_clock.clock,
        sleep=deterministic_clock.sleep,
    )
    stale = ServerConfig(name="hvac", transport=StdioTransport(kind="stdio", command="old-hvac-cmd"))
    candidate = _candidate().model_copy(update={"server_binding_identity": stale.binding_identity})

    record, outcome = await verifier.verify(candidate, _contract(), _executing_record(), now=NOW)

    assert outcome.state is ActionState.ESCALATED
    assert record.state is ActionState.ESCALATED
    assert outcome.observations == 0
    assert read_log == []
    assert "re-pointed" in outcome.reason
