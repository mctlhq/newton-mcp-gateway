import json

import pytest

from newton_mcp.action.contract import PhysicalActionContract
from newton_mcp.action.examples import MOCK_CONTRACT_EXAMPLE
from newton_mcp.action.prompts import CONTRACT_PROMPT_MARKER, build_contract_system_prompt
from newton_mcp.action.propose import propose_action
from newton_mcp.config import Settings
from newton_mcp.server import create_server

from conftest import ScriptedNewtonBackend, call_tool

VALID_CONTRACT = json.loads(json.dumps(MOCK_CONTRACT_EXAMPLE))
EXPECTED_ENVELOPE_KEYS = {"status", "contract", "raw_text", "errors", "backend", "observation_id"}


def _completed(*outputs):
    return {"status": "completed", "outputs": list(outputs)}


def _failed(outputs=None, error=None):
    return {"status": "failed", "outputs": outputs or [], "error": error}


# --- T2: prompt --------------------------------------------------------------


def test_prompt_contains_schema_marker_goals_and_no_fences_instruction():
    schema_json = json.dumps(PhysicalActionContract.model_json_schema(), indent=2, sort_keys=True)
    prompt = build_contract_system_prompt(("a_goal", "b_goal"))
    assert schema_json in prompt
    assert "a_goal" in prompt
    assert "b_goal" in prompt
    assert CONTRACT_PROMPT_MARKER in prompt
    assert "no markdown fences" in prompt
    assert "locks" in prompt and "ovens" in prompt and "alarms" in prompt


def test_prompt_omits_goal_section_when_no_allowed_goals():
    prompt = build_contract_system_prompt(())
    assert "allowed goals" not in prompt.lower()


# --- T3: first attempt succeeds -----------------------------------------------


async def test_valid_output_first_attempt_completes():
    backend = ScriptedNewtonBackend([_completed(json.dumps(VALID_CONTRACT))])
    result = await propose_action(backend, model="Newton::test", text_events=["kitchen is hot"])
    assert result.status == "completed"
    assert result.contract is not None
    assert result.raw_text is None
    assert result.errors == []
    assert len(backend.requests) == 1


# --- T4: invalid JSON then valid on retry -------------------------------------


async def test_invalid_json_then_valid_completes_on_retry():
    backend = ScriptedNewtonBackend([
        _completed("not json"),
        _completed(json.dumps(VALID_CONTRACT)),
    ])
    result = await propose_action(backend, model="Newton::test", text_events=["kitchen is hot"])
    assert result.status == "completed"
    assert len(backend.requests) == 2
    second = backend.requests[1]
    assert "invalid_json" in second.system_prompt
    assert "invalid_json" in second.instruction_prompt


# --- T5: invalid JSON on both attempts -----------------------------------------


async def test_invalid_json_both_attempts_fails():
    backend = ScriptedNewtonBackend([
        _completed("nope1"),
        _completed("nope2"),
    ])
    result = await propose_action(backend, model="Newton::test", text_events=["kitchen is hot"])
    assert result.status == "failed"
    assert result.contract is None
    assert result.raw_text == "nope2"
    kinds = [(e.attempt, e.kind) for e in result.errors]
    assert (1, "invalid_json") in kinds
    assert (2, "invalid_json") in kinds
    assert len(backend.requests) == 2


# --- T6: goal outside allowed_goals on both attempts ----------------------------


async def test_goal_not_allowed_both_attempts_fails():
    backend = ScriptedNewtonBackend([
        _completed(json.dumps(VALID_CONTRACT)),
        _completed(json.dumps(VALID_CONTRACT)),
    ])
    result = await propose_action(
        backend, model="Newton::test", text_events=["kitchen is hot"], allowed_goals=["turn_on_light"]
    )
    assert result.status == "failed"
    assert result.contract is None
    assert len(backend.requests) == 2
    assert len(result.errors) == 2
    for e in result.errors:
        assert e.kind == "goal_not_allowed"
        assert "reduce_room_temperature" in e.message
        assert "turn_on_light" in e.message


# --- T7: schema-invalid JSON on both attempts -----------------------------------


async def test_schema_invalid_json_both_attempts_fails_with_loc():
    bad = dict(VALID_CONTRACT)
    bad["confidence"] = 1.4
    bad.pop("verification")
    backend = ScriptedNewtonBackend([
        _completed(json.dumps(bad)),
        _completed(json.dumps(bad)),
    ])
    result = await propose_action(backend, model="Newton::test", text_events=["kitchen is hot"])
    assert result.status == "failed"
    assert result.contract is None
    kinds = {e.kind for e in result.errors}
    assert kinds == {"validation_error"}
    locs = [e.loc or "" for e in result.errors]
    assert any("confidence" in loc for loc in locs)
    assert any("verification" in loc for loc in locs)


# --- T8: mock path -------------------------------------------------------------


async def test_mock_path_returns_labelled_contract(mock_backend):
    settings = Settings()
    server = create_server(settings, backend=mock_backend)
    result = await call_tool(
        server, settings, mock_backend, "newton_propose_action", {"text_events": ["kitchen is hot"]}
    )
    payload = result.structured_content
    assert payload["status"] == "completed"
    assert payload["backend"] == "mock"
    contract = dict(payload["contract"])
    assert contract["reason"] == "[mock] " + MOCK_CONTRACT_EXAMPLE["reason"]

    expected = PhysicalActionContract.model_validate(
        {**MOCK_CONTRACT_EXAMPLE, "reason": "[mock] " + MOCK_CONTRACT_EXAMPLE["reason"]}
    ).model_dump()
    contract.pop("evidence")
    expected.pop("evidence")
    assert contract == expected


# --- T9: evidence overwritten from observation ----------------------------------


async def test_evidence_overwritten_from_observation_ignoring_model_supplied_evidence():
    model_supplied = dict(VALID_CONTRACT)
    model_supplied["evidence"] = {"observation_id": "forged", "summary": "forged summary"}
    backend = ScriptedNewtonBackend([_completed(json.dumps(model_supplied))])
    result = await propose_action(
        backend,
        model="Newton::test",
        text_events=["kitchen is hot"],
        json_events=['{"temperature_c": 29.4}'],
        observation_id="obs-explicit",
    )
    assert result.status == "completed"
    assert result.contract.evidence.observation_id == "obs-explicit"
    assert "kitchen is hot" in result.contract.evidence.summary
    assert "forged" not in result.contract.evidence.summary


# --- T10: observation_id handling -----------------------------------------------


async def test_observation_id_supplied_is_echoed_verbatim():
    backend = ScriptedNewtonBackend([_completed(json.dumps(VALID_CONTRACT))])
    result = await propose_action(backend, model="Newton::test", text_events=["x"], observation_id="obs-custom-1")
    assert result.observation_id == "obs-custom-1"
    assert result.contract.evidence.observation_id == "obs-custom-1"


async def test_observation_id_omitted_is_deterministic_and_content_derived():
    backend1 = ScriptedNewtonBackend([_completed(json.dumps(VALID_CONTRACT))])
    result1 = await propose_action(backend1, model="Newton::test", text_events=["same observation"])

    backend2 = ScriptedNewtonBackend([_completed(json.dumps(VALID_CONTRACT))])
    result2 = await propose_action(backend2, model="Newton::test", text_events=["same observation"])

    assert result1.observation_id == result2.observation_id
    assert result1.observation_id.startswith("obs-")
    assert len(result1.observation_id) == len("obs-") + 16

    backend3 = ScriptedNewtonBackend([_completed(json.dumps(VALID_CONTRACT))])
    result3 = await propose_action(backend3, model="Newton::test", text_events=["different observation"])
    assert result3.observation_id != result1.observation_id


# --- T11: pre-flight ValueErrors make zero backend calls ------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"text_events": ["   ", ""]},
        {"text_events": ["ok"], "json_events": ["not json"]},
        {"text_events": ["ok"], "allowed_goals": []},
        {"text_events": ["ok"], "allowed_goals": ["  "]},
    ],
)
async def test_preflight_value_errors_make_zero_backend_calls(kwargs):
    backend = ScriptedNewtonBackend([])
    with pytest.raises(ValueError):
        await propose_action(backend, model="Newton::test", **kwargs)
    assert backend.requests == []


# --- T12: envelope shape ---------------------------------------------------------


async def test_envelope_keys_on_success():
    backend = ScriptedNewtonBackend([_completed(json.dumps(VALID_CONTRACT))])
    result = await propose_action(backend, model="Newton::test", text_events=["x"])
    assert set(result.model_dump().keys()) == EXPECTED_ENVELOPE_KEYS


async def test_envelope_keys_on_failure():
    backend = ScriptedNewtonBackend([_completed("bad"), _completed("bad")])
    result = await propose_action(backend, model="Newton::test", text_events=["x"])
    assert set(result.model_dump().keys()) == EXPECTED_ENVELOPE_KEYS


# --- T13: empty/non-string outputs classified and retried once ------------------


@pytest.mark.parametrize(
    "bad_output,expected_kind",
    [
        ([], "empty_output"),
        ([None], "not_a_string"),
        ([{"a": 1}], "not_a_string"),
    ],
)
async def test_empty_or_non_string_outputs_classified_and_retried_once(bad_output, expected_kind):
    backend = ScriptedNewtonBackend([
        {"status": "completed", "outputs": bad_output},
        {"status": "completed", "outputs": bad_output},
    ])
    result = await propose_action(backend, model="Newton::test", text_events=["x"])
    assert result.status == "failed"
    assert len(backend.requests) == 2
    kinds = {e.kind for e in result.errors}
    assert kinds == {expected_kind}


# --- T14: tools/list traceability -------------------------------------------------


async def test_tools_list_includes_propose_action_read_only(server):
    tools = {t.name: t for t in await server.list_tools()}
    assert "newton_propose_action" in tools
    assert tools["newton_propose_action"].annotations.read_only_hint is True


# --- T15: allowed_goals normalisation ----------------------------------------------


async def test_allowed_goals_normalisation_dedupes_strips_preserves_order():
    backend = ScriptedNewtonBackend([
        _completed(json.dumps(VALID_CONTRACT)),
        _completed(json.dumps(VALID_CONTRACT)),
    ])
    await propose_action(
        backend,
        model="Newton::test",
        text_events=["x"],
        allowed_goals=["  b_goal ", "a_goal", "b_goal", " a_goal"],
    )
    prompt = backend.requests[0].system_prompt
    assert prompt.count("b_goal") == 1
    assert prompt.count("a_goal") == 1
    assert prompt.index("b_goal") < prompt.index("a_goal")


# --- T17: backend failure is terminal ----------------------------------------------


async def test_backend_failure_is_terminal_with_error_message():
    backend = ScriptedNewtonBackend([_failed(outputs=[], error="upstream timeout")])
    result = await propose_action(backend, model="Newton::test", text_events=["x"])
    assert result.status == "failed"
    assert result.contract is None
    assert result.raw_text is None
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.kind == "backend_failed"
    assert error.attempt == 1
    assert error.message == "upstream timeout"
    assert len(backend.requests) == 1


async def test_backend_failure_after_invalid_json_uses_fixed_no_error_message():
    backend = ScriptedNewtonBackend([
        _completed("not json"),
        _failed(outputs=[], error=None),
    ])
    result = await propose_action(backend, model="Newton::test", text_events=["x"])
    assert result.status == "failed"
    kinds = [(e.attempt, e.kind) for e in result.errors]
    assert kinds == [(1, "invalid_json"), (2, "backend_failed")]
    assert result.errors[1].message == "backend reported status=failed without an error message"
    assert len(backend.requests) == 2


# --- T18: mock + incompatible allowed_goals -----------------------------------------


async def test_mock_with_incompatible_allowed_goals_fails(mock_backend):
    settings = Settings()
    server = create_server(settings, backend=mock_backend)
    result = await call_tool(
        server,
        settings,
        mock_backend,
        "newton_propose_action",
        {"text_events": ["kitchen is hot"], "allowed_goals": ["turn_on_light"]},
    )
    payload = result.structured_content
    assert payload["status"] == "failed"
    assert payload["backend"] == "mock"
    assert payload["contract"] is None
    goal_errors = [e for e in payload["errors"] if e["kind"] == "goal_not_allowed"]
    assert len(goal_errors) == 2
    for e in goal_errors:
        assert "reduce_room_temperature" in e["message"]
        assert "turn_on_light" in e["message"]
