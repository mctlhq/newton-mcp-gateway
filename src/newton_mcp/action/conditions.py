"""Structured success conditions for `Verification.condition` (contract v0.2).

A `Condition` is exactly one of a predicate object `{path, op, value}`, an
`{all: [Condition, ...]}` conjunction, or an `{any: [Condition, ...]}`
disjunction -- resolved through a callable `Discriminator`, not a `kind`
field, so the wire shape stays exactly what the issue specifies. There is no
expression language and no parser anywhere in this module: `evaluate()` is a
pure function over a `Condition` and an observed mapping, performs no I/O,
consults no LLM, and never raises for a malformed comparison -- every failure
path returns `ConditionResult(satisfied=False, reason=...)`.

This module lives under `action/`, not `runtime/`, because it is part of the
contract model, and `action/` must never import `runtime/` (see
`canonical.py`'s docstring and `docs/action-runtime.md`).
"""

from __future__ import annotations

import operator
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, model_validator

#: A hostile or runaway model-authored condition (deep `all`/`any` nesting) is
#: bounded here, in the spirit of `MAX_TOOL_PAGES` in `runtime/catalog.py`.
MAX_CONDITION_DEPTH = 8


class Op(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


#: A predicate's `value` is a JSON scalar -- never a container. `bool` comes
#: before `int`/`float` in the union so pydantic's smart-union keeps `true`
#: a `bool` rather than coercing it to `1`.
Scalar = Union[bool, int, float, str, None]

_NUMERIC_TYPES = (int, float)

_COMPARATORS = {
    Op.EQ: operator.eq,
    Op.NE: operator.ne,
    Op.LT: operator.lt,
    Op.LE: operator.le,
    Op.GT: operator.gt,
    Op.GE: operator.ge,
}


class Predicate(BaseModel):
    """A single leaf comparison: observed value at `path` vs `value` under `op`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    op: Op
    value: Scalar


def _depth(condition: "Condition") -> int:
    """The nesting depth of `condition`: `1` for a leaf `Predicate`."""
    if isinstance(condition, Predicate):
        return 1
    children = condition.all if isinstance(condition, AllOf) else condition.any
    return 1 + max((_depth(child) for child in children), default=0)


def _enforce_max_depth(condition: "AllOf | AnyOf") -> None:
    depth = _depth(condition)
    if depth > MAX_CONDITION_DEPTH:
        raise ValueError(
            f"condition nesting depth {depth} exceeds MAX_CONDITION_DEPTH={MAX_CONDITION_DEPTH}"
        )


class AllOf(BaseModel):
    """Satisfied only if every child condition is satisfied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    all: tuple["Condition", ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_depth(self) -> "AllOf":
        _enforce_max_depth(self)
        return self


class AnyOf(BaseModel):
    """Satisfied if at least one child condition is satisfied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    any: tuple["Condition", ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_depth(self) -> "AnyOf":
        _enforce_max_depth(self)
        return self


def _condition_tag(value: Any) -> str:
    """Callable discriminator: `"all"`/`"any"` if that key/attribute is present, else `"predicate"`.

    The wire shape the issue fixes has no `kind`/tag field, so a callable
    `Discriminator` (not a field discriminator) is the only tool that fits:
    it still gives single-branch validation errors instead of ambiguous
    smart-union matching.
    """
    if isinstance(value, dict):
        if "all" in value:
            return "all"
        if "any" in value:
            return "any"
        return "predicate"
    if isinstance(value, AllOf):
        return "all"
    if isinstance(value, AnyOf):
        return "any"
    return "predicate"


Condition = Annotated[
    Union[
        Annotated[Predicate, Tag("predicate")],
        Annotated[AllOf, Tag("all")],
        Annotated[AnyOf, Tag("any")],
    ],
    Discriminator(_condition_tag),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()


class ConditionResult(BaseModel):
    """The result of `evaluate()`: a `satisfied` boolean and a non-empty `reason`."""

    model_config = ConfigDict(frozen=True)

    satisfied: bool
    reason: str


def resolve_path(path: str, observation: Mapping[str, Any]) -> tuple[bool, Any]:
    """Resolve a dotted `path` into `observation`, mapping traversal only (v0.2).

    Returns `(True, value)` when every segment resolves through a `Mapping`;
    `(False, None)` when a segment is missing, `None` is encountered
    mid-path, or a non-mapping (including a list -- there is no list-index
    traversal in v0.2) is encountered before the path is exhausted.
    """
    current: Any = observation
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _comparable_for_eq_ne(observed: Any, expected: Any) -> bool:
    """`eq`/`ne` compare only type-compatible operands; `bool` is never a number.

    Mirrors the rule `action/policy.py` already applies to `arg_ranges`.
    """
    if _is_bool(observed) or _is_bool(expected):
        return _is_bool(observed) and _is_bool(expected)
    if isinstance(observed, _NUMERIC_TYPES) and isinstance(expected, _NUMERIC_TYPES):
        return True
    if isinstance(observed, str) and isinstance(expected, str):
        return True
    if observed is None and expected is None:
        return True
    return False


def _comparable_for_ordering(observed: Any, expected: Any) -> bool:
    """`lt|le|gt|ge` require both operands to be non-bool `int`/`float`."""
    if _is_bool(observed) or _is_bool(expected):
        return False
    return isinstance(observed, _NUMERIC_TYPES) and isinstance(expected, _NUMERIC_TYPES)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__  # pragma: no cover - observations are JSON-shaped


def _evaluate_predicate(predicate: Predicate, observation: Mapping[str, Any]) -> ConditionResult:
    found, observed = resolve_path(predicate.path, observation)
    op = predicate.op
    expected = predicate.value

    if not found:
        return ConditionResult(
            satisfied=False,
            reason=f"path {predicate.path!r} not found in observation (op={op.value}, expected value={expected!r})",
        )

    comparable = (
        _comparable_for_eq_ne(observed, expected)
        if op in (Op.EQ, Op.NE)
        else _comparable_for_ordering(observed, expected)
    )
    observed_type = _type_name(observed)

    if not comparable:
        return ConditionResult(
            satisfied=False,
            reason=(
                f"path {predicate.path!r} type mismatch: observed value has type {observed_type}, "
                f"not comparable under op={op.value} against expected value {expected!r}"
            ),
        )

    satisfied = _COMPARATORS[op](observed, expected)
    return ConditionResult(
        satisfied=satisfied,
        reason=(
            f"path {predicate.path!r} op={op.value} expected value={expected!r} "
            f"(observed type={observed_type}): {'satisfied' if satisfied else 'not satisfied'}"
        ),
    )


def evaluate(condition: "Condition", observation: Mapping[str, Any]) -> ConditionResult:
    """Evaluate `condition` against `observation`. Pure: no I/O, no LLM, never raises.

    `all` is satisfied only if every child is; `any` is satisfied if at least
    one child is. The composite reason names the first unsatisfied child
    (`all`) or notes that no child matched (`any`), so a reason is always the
    most specific true one.
    """
    if isinstance(condition, Predicate):
        return _evaluate_predicate(condition, observation)

    if isinstance(condition, AllOf):
        for child in condition.all:
            result = evaluate(child, observation)
            if not result.satisfied:
                return ConditionResult(satisfied=False, reason=f"all: {result.reason}")
        return ConditionResult(satisfied=True, reason="all: every child condition was satisfied")

    if isinstance(condition, AnyOf):
        last_reason = "any: no child conditions"
        for child in condition.any:
            result = evaluate(child, observation)
            if result.satisfied:
                return ConditionResult(satisfied=True, reason=f"any: {result.reason}")
            last_reason = result.reason
        return ConditionResult(satisfied=False, reason=f"any: no child condition was satisfied ({last_reason})")

    raise TypeError(f"unknown condition type {type(condition)!r}")  # pragma: no cover - union is exhaustive
