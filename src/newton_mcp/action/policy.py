"""Minimal, deterministic policy engine for Physical Action Contracts.

Physical actions are not ordinary tool calls: a successful MCP response does
not mean the physical outcome happened, and some actions must never run
unattended. This module only decides AUTO / CONFIRM / DENY; execution and
verification live in later phases.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from newton_mcp.action.contract import PhysicalActionContract, Risk


class Decision(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


class PolicyRule(BaseModel):
    """First matching rule wins. A rule with no ``goal_prefix`` matches any goal."""

    goal_prefix: str | None = None
    max_risk: Risk = Risk.LOW
    decision: Decision = Decision.AUTO
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


_RISK_ORDER = [Risk.READ_ONLY, Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL]


class PolicyResult(BaseModel):
    decision: Decision
    reason: str


class Policy(BaseModel):
    rules: list[PolicyRule] = Field(default_factory=list)
    default: Decision = Decision.DENY

    @classmethod
    def conservative(cls) -> "Policy":
        """Default policy: read-only/low risk auto, medium confirm, high+ deny."""
        return cls(
            rules=[
                PolicyRule(max_risk=Risk.LOW, decision=Decision.AUTO, min_confidence=0.8),
                PolicyRule(max_risk=Risk.MEDIUM, decision=Decision.CONFIRM),
            ],
            default=Decision.DENY,
        )

    def evaluate(self, contract: PhysicalActionContract) -> PolicyResult:
        if contract.risk is Risk.CRITICAL:
            return PolicyResult(decision=Decision.DENY, reason="critical risk is never automated")
        if contract.requires_confirmation:
            return PolicyResult(decision=Decision.CONFIRM, reason="contract requests confirmation")
        for rule in self.rules:
            if rule.goal_prefix and not contract.goal.startswith(rule.goal_prefix):
                continue
            if _RISK_ORDER.index(contract.risk) > _RISK_ORDER.index(rule.max_risk):
                continue
            if contract.confidence is not None and contract.confidence < rule.min_confidence:
                continue
            return PolicyResult(decision=rule.decision, reason=f"matched rule max_risk={rule.max_risk}")
        return PolicyResult(decision=self.default, reason="no rule matched; default applied")
