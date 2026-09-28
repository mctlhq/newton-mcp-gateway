from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
from newton_mcp.action.policy import (
    POLICY_PATH_ENV_VAR,
    Decision,
    Policy,
    PolicyRule,
    ValueRange,
    load_policy,
)

MINIMAL_POLICY: dict = {
    "policy_version": "test.v1",
    "default": "deny",
    "rules": [
        {
            "name": "hvac-within-comfort-band",
            "goal_prefix": "reduce_room_temperature",
            "tool_name": "set_target_temperature",
            "max_risk": "low",
            "min_confidence": 0.8,
            "arg_ranges": {"target_temperature_c": {"min": 20, "max": 25}},
            "decision": "auto",
        }
    ],
}


@dataclass
class FakeCandidate:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    server_identity: str = "hvac-controller"
    server_binding_identity: str = "hvac-controller@sha256:deadbeef"


def _contract(
    *,
    goal: str = "reduce_room_temperature",
    risk: Risk | str = Risk.LOW,
    confidence: float | None = 0.96,
    requires_confirmation: bool | None = None,
) -> PhysicalActionContract:
    return PhysicalActionContract(
        goal=goal,
        reason="test",
        confidence=confidence,
        target=Target(type="environment", location="kitchen"),
        constraints={},
        risk=risk,
        requires_confirmation=requires_confirmation,
        verification=Verification(condition="temperature_c <= 24"),
    )


# ---------------------------------------------------------------------------
# T10-T15: policy YAML loading and schema
# ---------------------------------------------------------------------------


def test_example_policy_loads_and_has_expected_version_and_rule_count() -> None:
    example_path = Path(__file__).resolve().parent.parent / "examples" / "policy.example.yaml"
    policy = load_policy(example_path)
    assert policy.policy_version == "example.v1"
    assert len(policy.rules) == 3


def test_missing_policy_version_raises_validation_error() -> None:
    data = copy.deepcopy(MINIMAL_POLICY)
    del data["policy_version"]
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_empty_policy_version_raises_validation_error() -> None:
    data = copy.deepcopy(MINIMAL_POLICY)
    data["policy_version"] = ""
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_unknown_document_level_key_is_rejected() -> None:
    data = copy.deepcopy(MINIMAL_POLICY)
    data["unknown_key"] = "surprise"
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_unknown_rule_level_key_is_rejected() -> None:
    data = copy.deepcopy(MINIMAL_POLICY)
    data["rules"][0]["unknown_key"] = "surprise"
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_load_policy_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_PATH_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match=POLICY_PATH_ENV_VAR):
        load_policy()


def test_load_policy_env_var_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(POLICY_PATH_ENV_VAR, "   ")
    with pytest.raises(ValueError, match=POLICY_PATH_ENV_VAR):
        load_policy()


def test_load_policy_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(ValueError, match=str(missing)):
        load_policy(missing)


def test_load_policy_malformed_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("rules: [this is: not: valid: yaml")
    with pytest.raises(ValueError, match=str(bad)):
        load_policy(bad)


def test_load_policy_reads_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_POLICY))
    policy = load_policy(path)
    assert isinstance(policy, Policy)
    assert policy.policy_version == "test.v1"


def test_load_policy_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_POLICY))
    monkeypatch.setenv(POLICY_PATH_ENV_VAR, str(path))
    policy = load_policy()
    assert policy.policy_version == "test.v1"


def test_value_range_requires_at_least_one_bound() -> None:
    with pytest.raises(ValidationError):
        ValueRange()


def test_value_range_rejects_min_greater_than_max() -> None:
    with pytest.raises(ValidationError):
        ValueRange(min=25, max=20)


# ---------------------------------------------------------------------------
# T16-T22: policy invariants
# ---------------------------------------------------------------------------


def test_critical_risk_denies_even_when_a_matching_rule_would_auto_approve() -> None:
    policy = Policy(
        policy_version="v1",
        rules=[PolicyRule(max_risk=Risk.CRITICAL, decision=Decision.AUTO)],
    )
    result = policy.evaluate(_contract(risk=Risk.CRITICAL))
    assert result.decision is Decision.DENY


def test_requires_confirmation_raises_auto_to_confirm_but_does_not_upgrade_deny() -> None:
    policy = Policy(
        policy_version="v1",
        rules=[PolicyRule(max_risk=Risk.LOW, decision=Decision.AUTO, min_confidence=0.8)],
        default=Decision.DENY,
    )
    low_result = policy.evaluate(_contract(risk=Risk.LOW, requires_confirmation=True))
    assert low_result.decision is Decision.CONFIRM

    high_result = policy.evaluate(_contract(risk=Risk.HIGH, requires_confirmation=True))
    assert high_result.decision is Decision.DENY


def test_first_match_wins() -> None:
    policy = Policy(
        policy_version="v1",
        rules=[
            PolicyRule(max_risk=Risk.LOW, decision=Decision.CONFIRM),
            PolicyRule(max_risk=Risk.LOW, decision=Decision.AUTO),
        ],
    )
    result = policy.evaluate(_contract(risk=Risk.LOW, confidence=None))
    assert result.decision is Decision.CONFIRM


def test_empty_rules_returns_default_and_default_is_deny_when_omitted() -> None:
    policy = Policy(policy_version="v1")
    assert policy.default is Decision.DENY
    result = policy.evaluate(_contract())
    assert result.decision is Decision.DENY


def test_tool_name_matches_exactly_and_case_sensitively() -> None:
    policy = Policy(
        policy_version="v1",
        rules=[PolicyRule(tool_name="set_light_state", decision=Decision.AUTO)],
    )
    candidate = FakeCandidate(tool_name="Set_Light_State")
    result = policy.evaluate(_contract(), candidate)
    assert result.decision is Decision.DENY  # falls through to default, case mismatch


def test_min_confidence_with_none_confidence_does_not_match() -> None:
    policy = Policy(
        policy_version="v1",
        rules=[PolicyRule(max_risk=Risk.LOW, min_confidence=0.5, decision=Decision.AUTO)],
        default=Decision.DENY,
    )
    result = policy.evaluate(_contract(risk=Risk.LOW, confidence=None))
    assert result.decision is Decision.DENY


def test_conservative_policy_matrix_still_passes() -> None:
    # Mirrors tests/test_action_contract.py::test_conservative_policy_matrix, which
    # must keep passing unmodified; this is a belt-and-braces duplicate here.
    p = Policy.conservative()
    assert p.evaluate(_contract(risk=Risk.LOW, confidence=0.96)).decision is Decision.AUTO
    assert p.evaluate(_contract(risk=Risk.LOW, confidence=0.5)).decision is Decision.CONFIRM
    assert p.evaluate(_contract(risk=Risk.MEDIUM)).decision is Decision.CONFIRM
    assert p.evaluate(_contract(risk=Risk.HIGH)).decision is Decision.DENY
    assert p.evaluate(_contract(risk=Risk.CRITICAL)).decision is Decision.DENY


# ---------------------------------------------------------------------------
# T23-T26: value ranges
# ---------------------------------------------------------------------------


def _hvac_policy() -> Policy:
    return Policy.model_validate(MINIMAL_POLICY)


def test_in_range_value_yields_rule_decision_including_bounds() -> None:
    policy = _hvac_policy()
    for value in (20, 23, 25):
        candidate = FakeCandidate(tool_name="set_target_temperature", args={"target_temperature_c": value})
        result = policy.evaluate(_contract(), candidate)
        assert result.decision is Decision.AUTO, f"value={value}"


def test_out_of_range_value_denies_and_is_not_rescued_by_a_later_broader_rule() -> None:
    policy = Policy(
        policy_version="v1",
        rules=[
            PolicyRule(
                tool_name="set_target_temperature",
                max_risk=Risk.LOW,
                arg_ranges={"target_temperature_c": ValueRange(min=20, max=25)},
                decision=Decision.AUTO,
            ),
            PolicyRule(tool_name="set_target_temperature", max_risk=Risk.LOW, decision=Decision.AUTO),
        ],
    )
    candidate = FakeCandidate(tool_name="set_target_temperature", args={"target_temperature_c": 26})
    result = policy.evaluate(_contract(), candidate)
    assert result.decision is Decision.DENY
    assert "target_temperature_c" in result.reason


@pytest.mark.parametrize("bad_args", [{}, {"target_temperature_c": "23"}, {"target_temperature_c": True}])
def test_missing_non_numeric_or_bool_argument_denies(bad_args: dict) -> None:
    policy = _hvac_policy()
    candidate = FakeCandidate(tool_name="set_target_temperature", args=bad_args)
    result = policy.evaluate(_contract(), candidate)
    assert result.decision is Decision.DENY


def test_tool_name_or_arg_ranges_rule_does_not_match_without_candidate() -> None:
    policy = _hvac_policy()
    result = policy.evaluate(_contract())
    assert result.decision is Decision.DENY
    assert result.decision is policy.default
