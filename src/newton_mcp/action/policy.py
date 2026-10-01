"""Deterministic policy engine for Physical Action Contracts.

Physical actions are not ordinary tool calls: a successful MCP response does
not mean the physical outcome happened, and some actions must never run
unattended. This module only decides AUTO / CONFIRM / DENY; execution and
verification live in later phases.

The policy itself lives in a reviewable YAML file (`NEWTON_MCP_POLICY_PATH`,
see `load_policy()`) rather than in code, following the same philosophy as
`runtime/config.py`: every model here forbids unknown keys, and a broken or
missing policy file fails loudly rather than silently falling back to a
default -- including `Policy.conservative()`, which is never an implicit
fallback.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from newton_mcp.action.approval import ResolvedCandidate
from newton_mcp.action.contract import PhysicalActionContract, Risk

POLICY_PATH_ENV_VAR = "NEWTON_MCP_POLICY_PATH"
CONSERVATIVE_POLICY_VERSION = "builtin.conservative.v1"

_RISK_ORDER = [Risk.READ_ONLY, Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL]
_NUMERIC_TYPES = (int, float)


class Decision(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


class ValueRange(BaseModel):
    """An inclusive `[min, max]` bound checked against one resolved argument."""

    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _require_a_bound_in_order(self) -> "ValueRange":
        if self.min is None and self.max is None:
            raise ValueError("arg_ranges entry must declare at least one of min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"arg_ranges entry has min ({self.min}) > max ({self.max})")
        return self


class PolicyRule(BaseModel):
    """First matching rule wins. A rule with no predicate for a field matches any value of it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    goal_prefix: str | None = None
    tool_name: str | None = None
    max_risk: Risk = Risk.LOW
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    arg_ranges: dict[str, ValueRange] = Field(default_factory=dict)
    decision: Decision = Decision.AUTO


class PolicyResult(BaseModel):
    decision: Decision
    reason: str
    matched_rule_name: str | None = None


class Policy(BaseModel):
    """A reviewable policy: a required `policy_version` plus an ordered list of rules.

    Construct via `load_policy()` from a YAML file, or `Policy.conservative()`
    for the built-in safe default. Direct construction requires
    `policy_version` explicitly -- there is no default version, because a
    policy with no version cannot be pinned by an `Approval`.
    """

    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(min_length=1)
    rules: list[PolicyRule] = Field(default_factory=list)
    default: Decision = Decision.DENY

    @classmethod
    def conservative(cls) -> "Policy":
        """Built-in default policy: read-only/low risk auto, medium confirm, high+ deny."""
        return cls(
            policy_version=CONSERVATIVE_POLICY_VERSION,
            rules=[
                PolicyRule(max_risk=Risk.LOW, decision=Decision.AUTO, min_confidence=0.8),
                PolicyRule(max_risk=Risk.MEDIUM, decision=Decision.CONFIRM),
            ],
            default=Decision.DENY,
        )

    def evaluate(
        self,
        contract: PhysicalActionContract,
        candidate: ResolvedCandidate | None = None,
    ) -> PolicyResult:
        """Decide auto/confirm/deny for `contract`, optionally against a resolved `candidate`.

        Order: `risk is CRITICAL` denies before any rule is consulted; then
        rules are walked in file order and the first match wins; then
        `self.default`; finally a confirmation ceiling is applied to whatever
        decision was reached -- `requires_confirmation` raises an `auto`
        outcome to `confirm` but never downgrades `confirm`/`deny`.
        """
        if contract.risk is Risk.CRITICAL:
            return PolicyResult(decision=Decision.DENY, reason="critical risk is never automated")

        for rule in self.rules:
            matched, terminal_deny = self._match_rule(rule, contract, candidate)
            if terminal_deny is not None:
                return self._apply_confirmation_ceiling(
                    terminal_deny.model_copy(update={"matched_rule_name": rule.name}), contract
                )
            if matched:
                reason = f"matched rule {rule.name!r}" if rule.name else f"matched rule max_risk={rule.max_risk}"
                return self._apply_confirmation_ceiling(
                    PolicyResult(decision=rule.decision, reason=reason, matched_rule_name=rule.name), contract
                )

        default_result = PolicyResult(decision=self.default, reason="no rule matched; default applied")
        return self._apply_confirmation_ceiling(default_result, contract)

    @staticmethod
    def _match_rule(
        rule: PolicyRule,
        contract: PhysicalActionContract,
        candidate: ResolvedCandidate | None,
    ) -> tuple[bool, PolicyResult | None]:
        """Return `(matched, terminal_deny)`.

        `terminal_deny` is set only by the `arg_ranges` check: a missing
        argument, a non-numeric value, or a value outside its declared bound
        must deny immediately rather than let a later, broader rule
        auto-approve the very value this rule forbade. Checks run in order:
        `goal_prefix`, `tool_name`, `max_risk`, `min_confidence`, then
        `arg_ranges` last, and only once every earlier predicate matched.
        """
        if rule.goal_prefix is not None and not contract.goal.startswith(rule.goal_prefix):
            return False, None

        if rule.tool_name is not None and (candidate is None or rule.tool_name != candidate.tool_name):
            return False, None

        if _RISK_ORDER.index(contract.risk) > _RISK_ORDER.index(rule.max_risk):
            return False, None

        if rule.min_confidence > 0 and (contract.confidence is None or contract.confidence < rule.min_confidence):
            return False, None

        if rule.arg_ranges:
            if candidate is None:
                return False, None
            for arg_name, value_range in rule.arg_ranges.items():
                if arg_name not in candidate.args:
                    return False, PolicyResult(
                        decision=Decision.DENY,
                        reason=f"argument {arg_name!r} is missing from the resolved action",
                    )
                value = candidate.args[arg_name]
                if isinstance(value, bool) or not isinstance(value, _NUMERIC_TYPES):
                    return False, PolicyResult(
                        decision=Decision.DENY,
                        reason=f"argument {arg_name!r} is not a number ({value!r})",
                    )
                if value_range.min is not None and value < value_range.min:
                    return False, PolicyResult(
                        decision=Decision.DENY,
                        reason=f"argument {arg_name!r}={value!r} is below the allowed minimum {value_range.min}",
                    )
                if value_range.max is not None and value > value_range.max:
                    return False, PolicyResult(
                        decision=Decision.DENY,
                        reason=f"argument {arg_name!r}={value!r} is above the allowed maximum {value_range.max}",
                    )

        return True, None

    @staticmethod
    def _apply_confirmation_ceiling(result: PolicyResult, contract: PhysicalActionContract) -> PolicyResult:
        """A ceiling, never an upgrade: `auto` -> `confirm` when confirmation is required; `confirm`/`deny` pass through."""
        if contract.requires_confirmation and result.decision is Decision.AUTO:
            return PolicyResult(
                decision=Decision.CONFIRM,
                reason=f"{result.reason}; raised to confirm because the contract requires confirmation",
                matched_rule_name=result.matched_rule_name,
            )
        return result


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and validate a policy YAML file into a `Policy`.

    Reads the path from `NEWTON_MCP_POLICY_PATH` when `path` is not given
    (blank is treated as unset). Never falls back to `Policy.conservative()`
    or any other default: a missing variable, a missing file, or unparseable
    YAML all raise `ValueError` naming the variable or path plus the
    underlying error; a document that fails the schema raises
    `pydantic.ValidationError`.
    """
    if path is None:
        raw = os.environ.get(POLICY_PATH_ENV_VAR)
        if raw is None or not raw.strip():
            raise ValueError(
                f"{POLICY_PATH_ENV_VAR} is unset or blank; set it to the path of a policy.yaml "
                "file, or pass an explicit path to load_policy()"
            )
        path = raw.strip()

    resolved = Path(path)
    if not resolved.is_file():
        raise ValueError(f"policy file not found: {resolved}")

    try:
        raw_text = resolved.read_text()
    except OSError as exc:
        raise ValueError(f"could not read policy file {resolved}: {exc}") from None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"policy file {resolved} is not parseable YAML: {exc}") from None

    return Policy.model_validate(data or {})
