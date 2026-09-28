"""Deterministic v0 resolver: rank catalog capabilities against a `PhysicalActionContract`.

`Resolver.resolve()` is synchronous, makes no network call of its own, and
consults no LLM -- "no I/O" is structural here, not a promise. See
docs/action-runtime.md for the filter chain and scoring constants.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from jsonschema import SchemaError
from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict
from referencing.exceptions import Unresolvable

from newton_mcp.action.contract import PhysicalActionContract
from newton_mcp.runtime.catalog import CapabilityCatalog, CatalogEntry

BASE_SCORE = 0.50
GOAL_WEIGHT = 0.20
LOCATION_BONUS = 0.20
READ_TOOL_BONUS = 0.10

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")

RejectionStage = Literal[
    "server_unavailable",
    "tool_missing",
    "goal_prefix",
    "target_type",
    "target_location",
    "template_error",
    "schema_mismatch",
]


class TemplateError(ValueError):
    """A `${path}` placeholder that names an unknown root or a missing constraint."""

    def __init__(self, placeholder: str, message: str) -> None:
        self.placeholder = placeholder
        super().__init__(message)


class CandidateAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    server_identity: str
    tool_name: str
    args: dict[str, Any]
    read_tool: str | None
    idempotent: bool
    score: float
    why: str


class Rejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    server_identity: str
    tool_name: str
    stage: RejectionStage
    detail: str


class Resolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidates: tuple[CandidateAction, ...] = ()
    rejections: tuple[Rejection, ...] = ()


def _template_root_value(root: str, contract: PhysicalActionContract) -> Any:
    if root == "goal":
        return contract.goal
    if root == "reason":
        return contract.reason
    if root == "confidence":
        return contract.confidence
    if root == "target.type":
        return contract.target.type
    if root == "target.location":
        return contract.target.location
    if root == "target.resource":
        return contract.target.resource
    if root.startswith("constraints."):
        key = root[len("constraints.") :]
        if key not in contract.constraints:
            raise TemplateError(root, f"contract has no constraints[{key!r}]")
        return contract.constraints[key]
    raise TemplateError(root, f"unknown template root {root!r}")


def render_arguments(value: Any, contract: PhysicalActionContract) -> Any:
    """Render `${path}` placeholders in a capability's `arguments` against `contract`.

    A value that is exactly `"${path}"` is replaced with the resolved value,
    preserving its JSON type. A placeholder embedded in a longer string is
    interpolated via its string form. Dicts and lists render recursively;
    anything else passes through unchanged. There is no escape sequence in v0.

    Raises:
        TemplateError: a placeholder names an unknown root, or a missing
            `constraints` key.
    """
    if isinstance(value, str):
        whole_match = _PLACEHOLDER_RE.fullmatch(value)
        if whole_match is not None:
            return _template_root_value(whole_match.group(1), contract)

        def _interpolate(match: "re.Match[str]") -> str:
            return str(_template_root_value(match.group(1), contract))

        return _PLACEHOLDER_RE.sub(_interpolate, value)
    if isinstance(value, dict):
        return {key: render_arguments(item, contract) for key, item in value.items()}
    if isinstance(value, list):
        return [render_arguments(item, contract) for item in value]
    return value


def _longest_matching_prefix(prefixes: tuple[str, ...], goal: str) -> str | None:
    matches = [prefix for prefix in prefixes if goal.startswith(prefix)]
    if not matches:
        return None
    return max(matches, key=len)


class Resolver:
    """Evaluates every configured capability against a contract, deterministically."""

    def __init__(self, catalog: CapabilityCatalog) -> None:
        self._catalog = catalog

    def resolve(self, contract: PhysicalActionContract) -> Resolution:
        snapshot = self._catalog.snapshot
        config = self._catalog.config
        servers_by_name = config.servers_by_name

        entries_by_key: dict[tuple[str, str], CatalogEntry] = {
            (entry.capability.server, entry.capability.tool): entry for entry in snapshot.entries
        }
        server_unavailable_detail: dict[str, str] = {}
        tool_missing_detail: dict[tuple[str, str], str] = {}
        for problem in snapshot.problems:
            if problem.kind == "server_unavailable":
                server_unavailable_detail[problem.server] = problem.detail
            elif problem.kind == "tool_missing" and problem.tool is not None:
                tool_missing_detail[(problem.server, problem.tool)] = problem.detail

        candidates: list[CandidateAction] = []
        rejections: list[Rejection] = []

        for capability in config.capabilities:
            server_config = servers_by_name[capability.server]
            server_identity = server_config.resolved_identity
            key = (capability.server, capability.tool)

            if capability.server in server_unavailable_detail:
                rejections.append(
                    Rejection(
                        server_identity=server_identity,
                        tool_name=capability.tool,
                        stage="server_unavailable",
                        detail=server_unavailable_detail[capability.server],
                    )
                )
                continue

            if key in tool_missing_detail:
                rejections.append(
                    Rejection(
                        server_identity=server_identity,
                        tool_name=capability.tool,
                        stage="tool_missing",
                        detail=tool_missing_detail[key],
                    )
                )
                continue

            entry = entries_by_key.get(key)
            if entry is None:
                # Consistent catalogs never reach this: a capability with no problem always
                # has an entry after refresh(). Guard for a catalog read before any refresh().
                rejections.append(
                    Rejection(
                        server_identity=server_identity,
                        tool_name=capability.tool,
                        stage="tool_missing",
                        detail=f"tool {capability.tool!r} not present in the catalog (no refresh() yet?)",
                    )
                )
                continue

            outcome = self._evaluate(entry, contract, server_identity)
            if isinstance(outcome, Rejection):
                rejections.append(outcome)
            else:
                candidates.append(outcome)

        candidates.sort(key=lambda candidate: (-candidate.score, candidate.server_identity, candidate.tool_name))
        return Resolution(candidates=tuple(candidates), rejections=tuple(rejections))

    def _evaluate(
        self, entry: CatalogEntry, contract: PhysicalActionContract, server_identity: str
    ) -> CandidateAction | Rejection:
        capability = entry.capability
        tool_name = capability.tool

        matched_prefix = _longest_matching_prefix(capability.goal_prefixes, contract.goal)
        if matched_prefix is None:
            return Rejection(
                server_identity=server_identity,
                tool_name=tool_name,
                stage="goal_prefix",
                detail=f"none of {list(capability.goal_prefixes)} is a prefix of goal {contract.goal!r}",
            )

        if capability.target.type != contract.target.type:
            return Rejection(
                server_identity=server_identity,
                tool_name=tool_name,
                stage="target_type",
                detail=(
                    f"capability target.type {capability.target.type!r} != "
                    f"contract target.type {contract.target.type!r}"
                ),
            )

        explicit_location_match = False
        if capability.target.locations:
            if contract.target.location is None:
                return Rejection(
                    server_identity=server_identity,
                    tool_name=tool_name,
                    stage="target_location",
                    detail=(
                        "contract has no target.location but capability requires one of "
                        f"{list(capability.target.locations)}"
                    ),
                )
            location = contract.target.location.casefold()
            if not any(location == candidate.casefold() for candidate in capability.target.locations):
                return Rejection(
                    server_identity=server_identity,
                    tool_name=tool_name,
                    stage="target_location",
                    detail=(
                        f"target.location {contract.target.location!r} does not match any of "
                        f"{list(capability.target.locations)}"
                    ),
                )
            explicit_location_match = True

        try:
            args = render_arguments(capability.arguments, contract)
        except TemplateError as exc:
            return Rejection(
                server_identity=server_identity,
                tool_name=tool_name,
                stage="template_error",
                detail=f"placeholder {exc.placeholder!r}: {exc}",
            )

        schema_error = self._schema_mismatch(entry.tool.input_schema, args)
        if schema_error is not None:
            return Rejection(
                server_identity=server_identity, tool_name=tool_name, stage="schema_mismatch", detail=schema_error
            )

        read_tool_discovered = entry.read_tool is not None
        score = BASE_SCORE
        score += GOAL_WEIGHT * (len(matched_prefix) / max(len(contract.goal), 1))
        score += LOCATION_BONUS if explicit_location_match else 0.0
        score += READ_TOOL_BONUS if read_tool_discovered else 0.0

        why = (
            f"goal prefix {matched_prefix!r} matched; "
            f"{'explicit location match' if explicit_location_match else 'wildcard location match'}; "
            f"{'read tool discovered' if read_tool_discovered else 'no read tool configured/discovered'} "
            f"(score={score:.2f})"
        )

        return CandidateAction(
            server_identity=server_identity,
            tool_name=tool_name,
            args=args,
            read_tool=capability.read_tool,
            idempotent=capability.idempotent,
            score=score,
            why=why,
        )

    @staticmethod
    def _schema_mismatch(schema: dict[str, Any], args: Any) -> str | None:
        """Return a validation-failure message, or `None` if `args` satisfies `schema`.

        An invalid `input_schema` on the discovered tool is itself reported as a
        mismatch, never raised out of `resolve()`.
        """
        try:
            validator_cls = validator_for(schema)
            validator_cls.check_schema(schema)
            validator = validator_cls(schema)
            error = best_match(validator.iter_errors(args))
        except SchemaError as exc:
            return f"tool input_schema is invalid: {exc}"
        except Unresolvable as exc:
            return f"tool input_schema has an unresolved $ref: {exc}"
        if error is not None:
            return error.message
        return None
