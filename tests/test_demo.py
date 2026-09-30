"""Tests for the smart-home testbed (`examples/smart-home/`): T1-T15 of the proposal.

`examples/` is not an importable package (`pyproject.toml` packages only
`src/newton_mcp`), so `demo.py` and `fake_alice.py` are loaded with
`importlib.util.spec_from_file_location`, exactly as the proposal's tasks.md
specifies. Mock mode only throughout this module: no test selects `--real`
with a live backend, opens a socket, or reads a credential.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import anyio
import pytest

from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
from newton_mcp.action.policy import Decision, load_policy
from newton_mcp.runtime.audit import SECRET_KEY_PATTERNS
from newton_mcp.runtime.catalog import CapabilityCatalog
from newton_mcp.runtime.config import load_runtime_config
from newton_mcp.runtime.lifecycle import ActionState

REPO_ROOT = Path(__file__).resolve().parent.parent
SMART_HOME_DIR = REPO_ROOT / "examples" / "smart-home"
DEMO_PATH = SMART_HOME_DIR / "demo.py"
FAKE_ALICE_PATH = SMART_HOME_DIR / "fake_alice.py"
RUNTIME_YAML_PATH = SMART_HOME_DIR / "runtime.yaml"
POLICY_YAML_PATH = SMART_HOME_DIR / "policy.yaml"
TRACE_PATH = SMART_HOME_DIR / "trace.jsonl"

_FORBIDDEN_SUBSTRINGS = ("lock", "oven", "alarm", "industrial", "safety", "start", "stop")


def _load_module(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def demo() -> ModuleType:
    """A fresh load of `examples/smart-home/demo.py` for each test.

    `demo.py` itself extends `sys.path` with its own directory and does a
    plain `import fake_alice`, independent of the `fake_alice` fixture
    below (which loads the same file under a different module name) -- the
    two never need to share class identity because no test passes a
    `RoomState` from one into the other.
    """
    return _load_module("_smart_home_demo_under_test", DEMO_PATH)


@pytest.fixture()
def fake_alice() -> ModuleType:
    return _load_module("_smart_home_fake_alice_under_test", FAKE_ALICE_PATH)


def _announce_contract() -> PhysicalActionContract:
    return PhysicalActionContract(
        goal="announce",
        reason="test: kitchen fire drill reminder",
        confidence=0.9,
        target=Target(type="environment"),
        constraints={},
        risk=Risk.LOW,
        verification=Verification(condition={"path": "x", "op": "eq", "value": 1}),
    )


# ---------------------------------------------------------------------------
# T1-T5: the demo run, driven through run_demo()
# ---------------------------------------------------------------------------


def test_t1_mock_run_succeeds_with_one_ac_call_and_four_ids(demo: ModuleType, tmp_path: Path) -> None:
    call_log: list[tuple[str, dict]] = []
    result = anyio.run(
        lambda: demo.run_demo(
            call_log=call_log,
            audit_path=tmp_path / "audit.jsonl",
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.SUCCEEDED
    ac_calls = [entry for entry in call_log if entry[0] == "set_ac_temperature"]
    read_polls = [entry for entry in call_log if entry[0] == "get_room_state"]
    assert len(ac_calls) == 1
    assert result.tool_call_count == 1
    assert result.read_poll_count == len(read_polls) == 3
    assert result.tool_call_count + result.read_poll_count == len(call_log)
    assert result.observation_id and result.action_id and result.tool_call_id and result.verification_id


def test_t2_documented_command_subprocess_exits_zero_and_prints_succeeded(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    proc = subprocess.run(
        [sys.executable, str(DEMO_PATH), "--mock", "--audit-path", str(audit_path)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUCCEEDED" in proc.stdout


def test_t3_ac_offline_escalates_with_at_least_one_observation_and_bounded_calls(
    demo: ModuleType, tmp_path: Path
) -> None:
    call_log: list[tuple[str, dict]] = []
    result = anyio.run(
        lambda: demo.run_demo(
            ac_offline=True,
            call_log=call_log,
            audit_path=tmp_path / "audit.jsonl",
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.ESCALATED
    ac_calls = [entry for entry in call_log if entry[0] == "set_ac_temperature"]
    read_polls = [entry for entry in call_log if entry[0] == "get_room_state"]
    # MOCK_CONTRACT_EXAMPLE carries verification.retry_limit == 1 and the AC capability is
    # idempotent, so exactly retry_limit + 1 == 2 actuator calls, never counted as read polls.
    assert len(ac_calls) == 2
    assert result.tool_call_count == 2
    assert result.read_poll_count == len(read_polls) == 20
    # A verification poll separates the two actuator calls.
    first, second = [i for i, entry in enumerate(call_log) if entry[0] == "set_ac_temperature"]
    assert any(entry[0] == "get_room_state" for entry in call_log[first + 1 : second])


def test_t4_announce_contract_escalates_with_exactly_one_call(demo: ModuleType, tmp_path: Path) -> None:
    call_log: list[tuple[str, dict]] = []
    result = anyio.run(
        lambda: demo.run_demo(
            contract_override=_announce_contract(),
            observation_id_override="obs-test-announce",
            call_log=call_log,
            audit_path=tmp_path / "audit.jsonl",
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.ESCALATED
    announce_calls = [entry for entry in call_log if entry[0] == "announce"]
    assert len(announce_calls) == 1
    assert len(call_log) == 1


def test_t5_out_of_band_temperature_is_denied_before_any_tool_call(
    demo: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    call_log: list[tuple[str, dict]] = []
    audit_path = tmp_path / "audit.jsonl"
    result = anyio.run(
        lambda: demo.run_demo(
            force_desired_temperature_c=30,
            call_log=call_log,
            audit_path=audit_path,
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.DENIED
    assert call_log == []
    assert result.tool_call_count == 0
    assert result.read_poll_count == 0
    assert "testbed override (not Newton output)" in capsys.readouterr().out

    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["from"] == "proposed"
    assert lines[0]["to"] == "denied"


# ---------------------------------------------------------------------------
# T6-T8: configuration files
# ---------------------------------------------------------------------------


@pytest.fixture()
def _ac_candidate_factory():
    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class _FakeCandidate:
        tool_name: str
        args: dict[str, Any] = field(default_factory=dict)
        server_identity: str = "alice"
        server_binding_identity: str = "alice@sha256:deadbeef"

    return _FakeCandidate


def _ac_contract() -> PhysicalActionContract:
    return PhysicalActionContract(
        goal="reduce_room_temperature",
        reason="test",
        confidence=0.96,
        target=Target(type="environment", location="kitchen"),
        constraints={},
        risk=Risk.LOW,
        verification=Verification(condition={"path": "temperature_c", "op": "le", "value": 24}),
    )


@pytest.mark.parametrize("value", [19, 26])
def test_t6_out_of_band_temperature_denies_naming_the_argument(value: int, _ac_candidate_factory) -> None:
    policy = load_policy(POLICY_YAML_PATH)
    candidate = _ac_candidate_factory(tool_name="set_ac_temperature", args={"target_temperature_c": value})
    result = policy.evaluate(_ac_contract(), candidate)
    assert result.decision is Decision.DENY
    assert "target_temperature_c" in result.reason


@pytest.mark.parametrize("value", [20, 23, 25])
def test_t6_in_band_temperature_is_auto_inclusive_bounds(value: int, _ac_candidate_factory) -> None:
    policy = load_policy(POLICY_YAML_PATH)
    candidate = _ac_candidate_factory(tool_name="set_ac_temperature", args={"target_temperature_c": value})
    result = policy.evaluate(_ac_contract(), candidate)
    assert result.decision is Decision.AUTO


def test_t7_runtime_yaml_validates_idempotency_and_is_free_of_forbidden_substrings() -> None:
    config = load_runtime_config(RUNTIME_YAML_PATH)
    by_tool = {capability.tool: capability for capability in config.capabilities}

    assert by_tool["set_ac_temperature"].idempotent is True
    assert by_tool["set_light_state"].idempotent is True
    assert by_tool["set_light_brightness"].idempotent is True
    assert by_tool["announce"].idempotent is False

    for capability in config.capabilities:
        haystacks = [capability.tool, *capability.goal_prefixes]
        for haystack in haystacks:
            lowered = haystack.lower()
            for forbidden in _FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, f"{haystack!r} contains forbidden substring {forbidden!r}"


def test_t8_runtime_yaml_capabilities_match_fake_alice_catalog_with_no_problems(fake_alice: ModuleType) -> None:
    state = fake_alice.RoomState(room="kitchen", temperature_c=25.0)
    server = fake_alice.build_fake_alice(state)
    client_factory = fake_alice.in_process_factory(server)

    config = load_runtime_config(RUNTIME_YAML_PATH)
    catalog = CapabilityCatalog(config, client_factory=client_factory)
    snapshot = anyio.run(catalog.refresh)

    assert snapshot.problems == ()
    assert len(snapshot.entries) == len(config.capabilities)


# ---------------------------------------------------------------------------
# T9: the committed trace.jsonl
# ---------------------------------------------------------------------------


def test_t9_committed_trace_is_a_clean_success_chain() -> None:
    lines = [json.loads(line) for line in TRACE_PATH.read_text().splitlines() if line.strip()]
    transitions = [f"{line['from']}->{line['to']}" for line in lines]
    assert transitions == [
        "proposed->authorized",
        "authorized->executing",
        "executing->executed",
        "executed->verifying",
        "verifying->succeeded",
    ]

    for line in lines:
        for id_field in ("observation_id", "action_id", "tool_call_id", "verification_id"):
            assert line[id_field], f"{id_field} is empty on line {line}"
        assert line["at"]
        _assert_no_unredacted_secret(line)


def _assert_no_unredacted_secret(obj) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and any(pattern in key.casefold() for pattern in SECRET_KEY_PATTERNS):
                assert value == "[redacted]", f"key {key!r} looks secret but was not redacted: {value!r}"
            _assert_no_unredacted_secret(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_unredacted_secret(item)


# ---------------------------------------------------------------------------
# T10: src/newton_mcp/ carries nothing Alice-specific
# ---------------------------------------------------------------------------


def test_t10_src_newton_mcp_contains_no_alice_specific_string() -> None:
    src_root = REPO_ROOT / "src" / "newton_mcp"
    banned = ("alice", "fake_alice", "alice_mcp_url")
    offenders = []
    for path in src_root.rglob("*.py"):
        text = path.read_text().lower()
        for term in banned:
            if term in text:
                offenders.append((path, term))
    assert offenders == [], f"src/newton_mcp/ contains Alice-specific strings: {offenders}"


# ---------------------------------------------------------------------------
# T11: fake_alice's read tool is annotated read-only and shaped correctly
# ---------------------------------------------------------------------------


def test_t11_get_room_state_is_read_only_and_shaped_for_the_mock_condition(fake_alice: ModuleType) -> None:
    state = fake_alice.RoomState(room="kitchen", temperature_c=29.4)
    server = fake_alice.build_fake_alice(state)
    client_factory = fake_alice.in_process_factory(server)

    config = load_runtime_config(RUNTIME_YAML_PATH)
    catalog = CapabilityCatalog(config, client_factory=client_factory)
    snapshot = anyio.run(catalog.refresh)

    ac_entry = next(entry for entry in snapshot.entries if entry.capability.tool == "set_ac_temperature")
    assert ac_entry.read_tool is not None
    assert ac_entry.read_tool.read_only_hint is True

    async def _read() -> dict:
        async with client_factory(config.servers[0]) as client:
            result = await client.call_tool("get_room_state", {"room": "kitchen"})
            return result.structured_content

    observation = anyio.run(_read)
    assert "temperature_c" in observation
    assert observation["temperature_c"] == pytest.approx(29.4) or isinstance(observation["temperature_c"], float)


# ---------------------------------------------------------------------------
# T12: mock mode opens no real transport and reads no credential
# ---------------------------------------------------------------------------


def test_t12_mock_mode_uses_only_the_in_process_fake_and_no_credential(
    demo: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATAI_API_KEY", raising=False)
    call_log: list[tuple[str, dict]] = []
    result = anyio.run(
        lambda: demo.run_demo(
            call_log=call_log,
            audit_path=tmp_path / "audit.jsonl",
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.SUCCEEDED
    # Every interaction landed in the one in-process fake's call_log, classified by tool.
    assert result.tool_call_count == sum(1 for entry in call_log if entry[0] == "set_ac_temperature") == 1
    assert result.read_poll_count == sum(1 for entry in call_log if entry[0] == "get_room_state")
    assert result.tool_call_count + result.read_poll_count == len(call_log)


# ---------------------------------------------------------------------------
# T13-T14: --real (owner amendment) -- real Newton backend, fake actuator
# ---------------------------------------------------------------------------


def test_t13_real_mode_with_monkeypatched_backend_uses_fake_actuator_only(
    demo: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from newton_mcp.newton.mock import MockNewtonBackend

    monkeypatch.delenv("ATAI_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_MCP_URL", raising=False)

    call_log: list[tuple[str, dict]] = []
    result = anyio.run(
        lambda: demo.run_demo(
            real=True,
            real_backend_factory=lambda: MockNewtonBackend(),
            call_log=call_log,
            audit_path=tmp_path / "audit.jsonl",
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.SUCCEEDED
    assert result.tool_call_count == sum(1 for entry in call_log if entry[0] == "set_ac_temperature") == 1
    assert result.read_poll_count == sum(1 for entry in call_log if entry[0] == "get_room_state")
    assert result.tool_call_count + result.read_poll_count == len(call_log)

    captured = capsys.readouterr()
    assert "live Newton backend" in captured.out
    assert "in-process fake" in captured.out


def test_t14_real_mode_without_credential_fails_before_proposing(
    demo: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATAI_API_KEY", raising=False)
    call_log: list[tuple[str, dict]] = []

    with pytest.raises(ValueError, match="ATAI_API_KEY"):
        anyio.run(
            lambda: demo.run_demo(
                real=True,
                call_log=call_log,
                audit_path=tmp_path / "audit.jsonl",
                overwrite=True,
            )
        )
    assert call_log == []


# ---------------------------------------------------------------------------
# T15: approval expiry covers the whole (deterministic) run
# ---------------------------------------------------------------------------


def test_t15_approval_expiry_covers_the_whole_deterministic_ac_offline_run(demo: ModuleType, tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    call_log: list[tuple[str, dict]] = []
    result = anyio.run(
        lambda: demo.run_demo(
            ac_offline=True,
            deterministic=True,
            call_log=call_log,
            audit_path=audit_path,
            overwrite=True,
        )
    )
    assert result.terminal_state is ActionState.ESCALATED
    assert result.approval_expires_at is not None

    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    assert lines, "expected at least one audit line"
    for line in lines:
        from datetime import datetime, timezone

        at = datetime.strptime(line["at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        assert at < result.approval_expires_at, f"transition at {at} is not before expires_at {result.approval_expires_at}"
        assert "no longer valid" not in line["reason"]
        assert "approval" not in line["reason"].lower() or "approved by" in line["reason"]

    # The run ended through a verified failure, never an approval rejection.
    assert lines[-1]["reason"].startswith("verified failure not retried")


def test_demo_uses_the_shared_default_text_model(demo: ModuleType) -> None:
    """One source of truth for the documented Newton model id (no copy in the demo)."""
    from newton_mcp.config import DEFAULT_TEXT_MODEL

    assert demo.PROPOSE_MODEL is DEFAULT_TEXT_MODEL
    assert "Newton::" not in DEMO_PATH.read_text()
