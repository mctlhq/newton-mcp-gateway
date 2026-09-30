from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from newton_mcp.action.conditions import (
    MAX_CONDITION_DEPTH,
    AllOf,
    AnyOf,
    Condition,
    Op,
    Predicate,
    evaluate,
    resolve_path,
)

_CONDITION = TypeAdapter(Condition)


def _predicate(path: str, op: str, value) -> Condition:
    return _CONDITION.validate_python({"path": path, "op": op, "value": value})


# ---------------------------------------------------------------------------
# T1: one case per operator, satisfied and unsatisfied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,observed,expected,satisfied",
    [
        ("eq", 5, 5, True),
        ("eq", 5, 6, False),
        ("ne", 5, 6, True),
        ("ne", 5, 5, False),
        ("lt", 4, 5, True),
        ("lt", 5, 5, False),
        ("le", 5, 5, True),
        ("le", 6, 5, False),
        ("gt", 6, 5, True),
        ("gt", 5, 5, False),
        ("ge", 5, 5, True),
        ("ge", 4, 5, False),
    ],
)
def test_each_operator_satisfied_and_unsatisfied(op: str, observed, expected, satisfied: bool) -> None:
    condition = _predicate("value", op, expected)
    result = evaluate(condition, {"value": observed})
    assert result.satisfied is satisfied
    assert result.reason


# ---------------------------------------------------------------------------
# T2: all/any nesting
# ---------------------------------------------------------------------------


def test_all_satisfied_only_when_every_child_is() -> None:
    condition = _CONDITION.validate_python(
        {"all": [{"path": "a", "op": "eq", "value": 1}, {"path": "b", "op": "eq", "value": 2}]}
    )
    assert evaluate(condition, {"a": 1, "b": 2}).satisfied is True

    result = evaluate(condition, {"a": 1, "b": 3})
    assert result.satisfied is False
    assert "'b'" in result.reason


def test_any_satisfied_when_at_least_one_child_is() -> None:
    condition = _CONDITION.validate_python(
        {"any": [{"path": "a", "op": "eq", "value": 1}, {"path": "b", "op": "eq", "value": 2}]}
    )
    assert evaluate(condition, {"a": 9, "b": 2}).satisfied is True

    result = evaluate(condition, {"a": 9, "b": 9})
    assert result.satisfied is False


def test_all_containing_any_containing_predicate() -> None:
    condition = _CONDITION.validate_python(
        {
            "all": [
                {"path": "a", "op": "eq", "value": 1},
                {"any": [{"path": "b", "op": "eq", "value": 2}, {"path": "c", "op": "eq", "value": 3}]},
            ]
        }
    )
    assert evaluate(condition, {"a": 1, "b": 9, "c": 3}).satisfied is True
    assert evaluate(condition, {"a": 1, "b": 9, "c": 9}).satisfied is False
    assert evaluate(condition, {"a": 9, "b": 2, "c": 3}).satisfied is False


# ---------------------------------------------------------------------------
# T3: missing path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", list(Op))
def test_missing_path_is_never_satisfied(op: Op) -> None:
    condition = _predicate("missing.path", op.value, 1)
    result = evaluate(condition, {"other": 1})
    assert result.satisfied is False
    assert "missing.path" in result.reason


def test_non_mapping_mid_path_segment_behaves_as_missing() -> None:
    condition = _predicate("a.b", "eq", 1)
    result = evaluate(condition, {"a": "not-a-mapping"})
    assert result.satisfied is False

    result_list = evaluate(condition, {"a": [1, 2, 3]})
    assert result_list.satisfied is False

    result_none = evaluate(condition, {"a": None})
    assert result_none.satisfied is False


def test_resolve_path_reports_found_and_value() -> None:
    assert resolve_path("a.b", {"a": {"b": 5}}) == (True, 5)
    assert resolve_path("a.b", {"a": {}}) == (False, None)
    assert resolve_path("a.b", {}) == (False, None)


# ---------------------------------------------------------------------------
# T4: type mismatches never raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", list(Op))
def test_string_vs_number_type_mismatch_never_raises(op: Op) -> None:
    condition = _predicate("value", op.value, 1)
    result = evaluate(condition, {"value": "not-a-number"})
    assert result.satisfied is False
    assert "type mismatch" in result.reason


def test_bool_vs_number_under_eq_is_type_mismatch() -> None:
    condition = _predicate("value", "eq", 1)
    result = evaluate(condition, {"value": True})
    assert result.satisfied is False
    assert "type mismatch" in result.reason


def test_bool_vs_number_under_ne_is_type_mismatch() -> None:
    condition = _predicate("value", "ne", 1)
    result = evaluate(condition, {"value": True})
    assert result.satisfied is False


@pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
def test_non_numeric_under_ordering_ops_is_type_mismatch(op: str) -> None:
    for observed in ("text", True, None):
        condition = _predicate("value", op, 5)
        result = evaluate(condition, {"value": observed})
        assert result.satisfied is False
        assert "type mismatch" in result.reason


def test_none_operands() -> None:
    condition = _predicate("value", "eq", None)
    assert evaluate(condition, {"value": None}).satisfied is True

    condition_ne = _predicate("value", "ne", None)
    assert evaluate(condition_ne, {"value": None}).satisfied is False

    mismatch = evaluate(_predicate("value", "eq", None), {"value": 1})
    assert mismatch.satisfied is False
    assert "type mismatch" in mismatch.reason


def test_no_reason_ever_echoes_the_raw_observed_value() -> None:
    sentinel = "sentinel-value-should-never-leak-13579"
    condition = _predicate("value", "eq", "expected-value")
    result = evaluate(condition, {"value": sentinel})
    assert sentinel not in result.reason


# ---------------------------------------------------------------------------
# T5: validation
# ---------------------------------------------------------------------------


def test_rejects_a_string_condition() -> None:
    with pytest.raises(ValidationError):
        _CONDITION.validate_python("temperature_c <= 24")


def test_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        _CONDITION.validate_python({"path": "a", "op": "eq", "value": 1, "surprise": True})


@pytest.mark.parametrize("key", ["all", "any"])
def test_rejects_empty_all_or_any(key: str) -> None:
    with pytest.raises(ValidationError):
        _CONDITION.validate_python({key: []})


def _nested(depth: int) -> dict:
    """Build an `all`-nested condition of exactly `depth` levels (a leaf predicate is depth 1)."""
    node: dict = {"path": "a", "op": "eq", "value": 1}
    for _ in range(depth - 1):
        node = {"all": [node]}
    return node


def test_nest_at_max_depth_is_accepted() -> None:
    _CONDITION.validate_python(_nested(MAX_CONDITION_DEPTH))


def test_nest_deeper_than_max_depth_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _CONDITION.validate_python(_nested(MAX_CONDITION_DEPTH + 1))


def test_model_dump_round_trips_wire_shape_and_value_types() -> None:
    predicate = _CONDITION.validate_python({"path": "a", "op": "eq", "value": 5})
    assert isinstance(predicate, Predicate)
    dumped = predicate.model_dump()
    assert dumped["path"] == "a"
    assert dumped["value"] == 5
    assert type(dumped["value"]) is int

    bool_predicate = _CONDITION.validate_python({"path": "a", "op": "eq", "value": True})
    assert type(bool_predicate.model_dump()["value"]) is bool

    str_predicate = _CONDITION.validate_python({"path": "a", "op": "eq", "value": "x"})
    assert type(str_predicate.model_dump()["value"]) is str

    all_condition = _CONDITION.validate_python({"all": [{"path": "a", "op": "eq", "value": 1}]})
    assert isinstance(all_condition, AllOf)
    assert all_condition.model_dump(mode="json") == {"all": [{"path": "a", "op": "eq", "value": 1}]}

    any_condition = _CONDITION.validate_python({"any": [{"path": "a", "op": "eq", "value": 1}]})
    assert isinstance(any_condition, AnyOf)
    assert any_condition.model_dump(mode="json") == {"any": [{"path": "a", "op": "eq", "value": 1}]}


def test_no_io_or_eval_or_parser_in_module() -> None:
    """Structural guard: the module source names no `eval`/`exec`/`compile` call."""
    import inspect

    from newton_mcp.action import conditions

    source = inspect.getsource(conditions)
    assert "eval(" not in source
    assert "exec(" not in source
    assert "compile(" not in source


@pytest.mark.parametrize("observation", [{}, {"a": "unavailable"}, {"a": float("nan")}, {"a": float("inf")}])
def test_missing_or_invalid_data_is_explicitly_unknown(observation) -> None:
    result = evaluate(_predicate("a", "le", 24), observation)
    assert result.satisfied is False
    assert result.known is False


@pytest.mark.parametrize("key", ["all", "any"])
@pytest.mark.parametrize("children_order", [False, True])
def test_unknown_child_outranks_negative_composite_result(key, children_order) -> None:
    children = [{"path": "a", "op": "eq", "value": 1}, {"path": "b", "op": "eq", "value": 2}]
    if children_order:
        children.reverse()
    result = evaluate(_CONDITION.validate_python({key: children}), {"a": 9})
    assert result.satisfied is False
    assert result.known is False


def test_any_positive_child_proves_success_despite_unknown_sibling() -> None:
    condition = _CONDITION.validate_python({"any": [{"path": "a", "op": "eq", "value": 1}, {"path": "b", "op": "eq", "value": 2}]})
    result = evaluate(condition, {"b": 2})
    assert result.known is True
    assert result.satisfied is True
