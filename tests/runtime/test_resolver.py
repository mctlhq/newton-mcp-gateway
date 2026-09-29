from __future__ import annotations

from pathlib import Path

import pytest

from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
from newton_mcp.action.examples import MOCK_CONTRACT_EXAMPLE
from newton_mcp.runtime.catalog import CapabilityCatalog
from newton_mcp.runtime.config import (
    CapabilityConfig,
    RuntimeConfig,
    ServerConfig,
    StdioTransport,
    TargetMatch,
    load_runtime_config,
)
from newton_mcp.runtime.resolver import (
    BASE_SCORE,
    GOAL_WEIGHT,
    LOCATION_BONUS,
    READ_TOOL_BONUS,
    Resolver,
    TemplateError,
    render_arguments,
)

from .conftest import FakeToolSpec, build_fake_server, in_memory_factory

EXAMPLE_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "examples" / "runtime.example.yaml"


def _contract(
    *,
    goal: str = "reduce_room_temperature",
    target_type: str = "environment",
    location: str | None = "kitchen",
    constraints: dict | None = None,
) -> PhysicalActionContract:
    return PhysicalActionContract(
        goal=goal,
        reason="test",
        target=Target(type=target_type, location=location),
        constraints=constraints or {"desired_temperature_c": 23},
        risk=Risk.LOW,
        verification=Verification(condition={"path": "temperature_c", "op": "le", "value": 24}),
    )


def _server(name: str) -> ServerConfig:
    return ServerConfig(name=name, transport=StdioTransport(kind="stdio", command=f"{name}-cmd"))


async def set_target_temperature(location: str, target_temperature_c: int) -> dict:
    return {}


async def get_room_temperature(location: str) -> dict:
    return {}


async def set_light_state(location: str, on: bool) -> dict:
    return {}


async def get_light_state(location: str) -> dict:
    return {}


async def announce(message: str) -> dict:
    return {}


async def _catalog_for(config: RuntimeConfig, fakes: dict) -> CapabilityCatalog:
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory(fakes))
    await catalog.refresh()
    return catalog


# ---------------------------------------------------------------------------
# T10: ranking and stability
# ---------------------------------------------------------------------------


async def test_ranking_prefers_exact_goal_and_location_and_read_tool() -> None:
    fake = build_fake_server(
        "hvac",
        [
            FakeToolSpec("set_target_temperature", set_target_temperature),
            FakeToolSpec("get_room_temperature", get_room_temperature),
            FakeToolSpec("set_target_temperature_wide", set_target_temperature),
        ],
    )
    config = RuntimeConfig(
        servers=(_server("hvac"),),
        capabilities=(
            CapabilityConfig(
                server="hvac",
                tool="set_target_temperature",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment", locations=("kitchen",)),
                arguments={"location": "${target.location}", "target_temperature_c": "${constraints.desired_temperature_c}"},
                read_tool="get_room_temperature",
            ),
            CapabilityConfig(
                server="hvac",
                tool="set_target_temperature_wide",
                goal_prefixes=("reduce",),
                target=TargetMatch(type="environment"),
                arguments={"location": "${target.location}", "target_temperature_c": "${constraints.desired_temperature_c}"},
            ),
        ),
    )
    catalog = await _catalog_for(config, {"hvac": fake})
    contract = _contract()

    result_1 = Resolver(catalog).resolve(contract)
    result_2 = Resolver(catalog).resolve(contract)

    assert len(result_1.candidates) == 2
    assert result_1.candidates[0].tool_name == "set_target_temperature"
    assert result_1.candidates[1].tool_name == "set_target_temperature_wide"
    assert result_1.candidates[0].score > result_1.candidates[1].score
    assert result_1.candidates == result_2.candidates


# ---------------------------------------------------------------------------
# T11: schema mismatch
# ---------------------------------------------------------------------------


async def test_schema_mismatch_wrong_type_is_rejected() -> None:
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", set_target_temperature)])
    config = RuntimeConfig(
        servers=(_server("hvac"),),
        capabilities=(
            CapabilityConfig(
                server="hvac",
                tool="set_target_temperature",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment"),
                # desired_temperature_c is an int in the contract; force a string by
                # embedding the placeholder in a longer string, so it stops satisfying
                # the tool's `integer` schema.
                arguments={"location": "${target.location}", "target_temperature_c": "value=${constraints.desired_temperature_c}"},
            ),
        ),
    )
    catalog = await _catalog_for(config, {"hvac": fake})

    result = Resolver(catalog).resolve(_contract())

    assert result.candidates == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].stage == "schema_mismatch"
    assert result.rejections[0].detail


async def test_schema_mismatch_missing_required_property_is_rejected() -> None:
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", set_target_temperature)])
    config = RuntimeConfig(
        servers=(_server("hvac"),),
        capabilities=(
            CapabilityConfig(
                server="hvac",
                tool="set_target_temperature",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment"),
                # target_temperature_c is a required property of the tool's schema
                # and is never supplied here.
                arguments={"location": "${target.location}"},
            ),
        ),
    )
    catalog = await _catalog_for(config, {"hvac": fake})

    result = Resolver(catalog).resolve(_contract())

    assert result.candidates == ()
    assert result.rejections[0].stage == "schema_mismatch"


# ---------------------------------------------------------------------------
# T12: every rejection stage, one per configured capability
# ---------------------------------------------------------------------------


async def test_every_configured_capability_gets_exactly_one_rejection_when_none_match() -> None:
    async def tool_a(x: str) -> dict:
        return {}

    fake_a = build_fake_server(
        "svc-a",
        [
            FakeToolSpec("tool_goal_mismatch", tool_a),
            FakeToolSpec("tool_type_mismatch", tool_a),
            FakeToolSpec("tool_location_mismatch", tool_a),
            # tool_missing_case is intentionally never registered.
        ],
    )
    config = RuntimeConfig(
        servers=(_server("svc-a"), _server("svc-b")),
        capabilities=(
            CapabilityConfig(
                server="svc-a",
                tool="tool_goal_mismatch",
                goal_prefixes=("goal_never_matches",),
                target=TargetMatch(type="environment"),
                arguments={"x": "literal"},
            ),
            CapabilityConfig(
                server="svc-a",
                tool="tool_type_mismatch",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="machine"),
                arguments={"x": "literal"},
            ),
            CapabilityConfig(
                server="svc-a",
                tool="tool_location_mismatch",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment", locations=("attic",)),
                arguments={"x": "literal"},
            ),
            CapabilityConfig(
                server="svc-a",
                tool="tool_missing_case",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment"),
                arguments={"x": "literal"},
            ),
            CapabilityConfig(
                server="svc-b",
                tool="tool_unreachable",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment"),
                arguments={"x": "literal"},
            ),
        ),
    )
    # svc-b is deliberately absent from the fake map, so its factory raises.
    catalog = await _catalog_for(config, {"svc-a": fake_a})

    result = Resolver(catalog).resolve(_contract())

    assert result.candidates == ()
    assert len(result.rejections) == 5
    stages = {r.tool_name: r.stage for r in result.rejections}
    assert stages == {
        "tool_goal_mismatch": "goal_prefix",
        "tool_type_mismatch": "target_type",
        "tool_location_mismatch": "target_location",
        "tool_missing_case": "tool_missing",
        "tool_unreachable": "server_unavailable",
    }


# ---------------------------------------------------------------------------
# T13a / T13: render_arguments()
# ---------------------------------------------------------------------------


def test_render_arguments_preserves_json_type_for_whole_string_placeholder() -> None:
    contract = _contract()
    assert render_arguments("${constraints.desired_temperature_c}", contract) == 23


def test_render_arguments_interpolates_embedded_placeholder() -> None:
    contract = _contract(goal="reduce_room_temperature")
    result = render_arguments("goal is ${goal} exactly", contract)
    assert result == "goal is reduce_room_temperature exactly"


def test_render_arguments_renders_nested_dicts_and_lists() -> None:
    contract = _contract()
    rendered = render_arguments(
        {"outer": {"inner": ["${target.location}", "${constraints.desired_temperature_c}"]}}, contract
    )
    assert rendered == {"outer": {"inner": ["kitchen", 23]}}


def test_render_arguments_missing_constraint_raises_template_error() -> None:
    contract = _contract()
    with pytest.raises(TemplateError) as exc_info:
        render_arguments("${constraints.does_not_exist}", contract)
    assert exc_info.value.placeholder == "constraints.does_not_exist"


@pytest.mark.parametrize("placeholder", ["${verification.condition}", "${verification.timeout_seconds}"])
def test_render_arguments_rejects_verification_as_unknown_root(placeholder: str) -> None:
    contract = _contract()
    with pytest.raises(TemplateError) as exc_info:
        render_arguments(placeholder, contract)
    assert exc_info.value.placeholder == placeholder.removeprefix("${").removesuffix("}")


# ---------------------------------------------------------------------------
# T14: CandidateAction fields and idempotent not affecting score
# ---------------------------------------------------------------------------


async def test_candidate_action_carries_read_tool_idempotent_and_why() -> None:
    fake = build_fake_server(
        "hvac", [FakeToolSpec("set_target_temperature", set_target_temperature), FakeToolSpec("get_room_temperature", get_room_temperature)]
    )
    config = RuntimeConfig(
        servers=(_server("hvac"),),
        capabilities=(
            CapabilityConfig(
                server="hvac",
                tool="set_target_temperature",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment", locations=("kitchen",)),
                arguments={"location": "${target.location}", "target_temperature_c": "${constraints.desired_temperature_c}"},
                read_tool="get_room_temperature",
                idempotent=True,
            ),
        ),
    )
    catalog = await _catalog_for(config, {"hvac": fake})

    result = Resolver(catalog).resolve(_contract())

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.read_tool == "get_room_temperature"
    assert candidate.idempotent is True
    assert candidate.why


async def test_idempotent_does_not_affect_score() -> None:
    async def tool_a(location: str) -> dict:
        return {}

    async def tool_b(location: str) -> dict:
        return {}

    fake = build_fake_server("hvac", [FakeToolSpec("tool_a", tool_a), FakeToolSpec("tool_b", tool_b)])
    config = RuntimeConfig(
        servers=(_server("hvac"),),
        capabilities=(
            CapabilityConfig(
                server="hvac",
                tool="tool_a",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment"),
                arguments={"location": "${target.location}"},
                idempotent=True,
            ),
            CapabilityConfig(
                server="hvac",
                tool="tool_b",
                goal_prefixes=("reduce_room_temperature",),
                target=TargetMatch(type="environment"),
                arguments={"location": "${target.location}"},
                idempotent=False,
            ),
        ),
    )
    catalog = await _catalog_for(config, {"hvac": fake})

    result = Resolver(catalog).resolve(_contract())

    assert len(result.candidates) == 2
    assert result.candidates[0].score == result.candidates[1].score


def test_score_constants_sum_to_one_at_maximum() -> None:
    assert BASE_SCORE + GOAL_WEIGHT + LOCATION_BONUS + READ_TOOL_BONUS == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# T15: the shipped example config resolves against the shipped example contract
# ---------------------------------------------------------------------------


async def test_example_config_resolves_against_example_contract() -> None:
    example_config = load_runtime_config(EXAMPLE_RUNTIME_CONFIG_PATH)

    hvac = build_fake_server(
        "hvac-controller",
        [
            FakeToolSpec("set_target_temperature", set_target_temperature),
            FakeToolSpec("get_room_temperature", get_room_temperature),
        ],
    )
    home_bridge = build_fake_server(
        "home-bridge",
        [
            FakeToolSpec("set_light_state", set_light_state),
            FakeToolSpec("get_light_state", get_light_state),
            FakeToolSpec("announce", announce),
        ],
    )
    catalog = await _catalog_for(example_config, {"hvac-controller": hvac, "home-bridge": home_bridge})

    contract = PhysicalActionContract.model_validate(MOCK_CONTRACT_EXAMPLE)
    result = Resolver(catalog).resolve(contract)

    assert len(result.candidates) >= 1
    assert result.candidates[0].tool_name == "set_target_temperature"
