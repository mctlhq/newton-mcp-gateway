import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from newton_mcp.action import Decision, PhysicalActionContract, Policy, Risk
from newton_mcp.action.examples import MOCK_CONTRACT_EXAMPLE

EXAMPLE = Path(__file__).parent.parent / "examples" / "physical-action.json"
SCHEMA = Path(__file__).parent.parent / "schemas" / "physical-action-contract.schema.json"


def test_example_contract_validates():
    c = PhysicalActionContract.model_validate_json(EXAMPLE.read_text())
    assert c.goal == "reduce_room_temperature"
    assert c.risk is Risk.LOW


def test_schema_file_is_in_sync_with_model():
    assert json.loads(SCHEMA.read_text()) == PhysicalActionContract.model_json_schema()


def test_mock_contract_example_is_in_sync_with_example_file():
    assert MOCK_CONTRACT_EXAMPLE == json.loads(EXAMPLE.read_text())


def test_confidence_bounds():
    data = json.loads(EXAMPLE.read_text())
    data["confidence"] = 1.4
    with pytest.raises(ValidationError):
        PhysicalActionContract.model_validate(data)


def _contract(**overrides):
    data = json.loads(EXAMPLE.read_text())
    data.update(overrides)
    return PhysicalActionContract.model_validate(data)


def test_conservative_policy_matrix():
    p = Policy.conservative()
    assert p.evaluate(_contract(risk="low", confidence=0.96)).decision is Decision.AUTO
    assert p.evaluate(_contract(risk="low", confidence=0.5)).decision is Decision.CONFIRM
    assert p.evaluate(_contract(risk="medium")).decision is Decision.CONFIRM
    assert p.evaluate(_contract(risk="high")).decision is Decision.DENY
    assert p.evaluate(_contract(risk="critical")).decision is Decision.DENY


def test_explicit_confirmation_overrides_auto():
    p = Policy.conservative()
    assert p.evaluate(_contract(risk="low", requires_confirmation=True)).decision is Decision.CONFIRM
