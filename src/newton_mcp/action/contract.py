"""Physical Action Contract v0.1 — an EXPERIMENTAL proposal from this project.

Not an Archetype standard. The contract separates *what should happen in the
physical world* (produced by a Newton Agent or a Newton /query with a strict
JSON system prompt) from *how it is done* (resolved against MCP tools by the
action runtime). See docs/action-runtime.md.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Risk(StrEnum):
    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Target(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(description="Kind of physical target, e.g. 'environment', 'machine', 'zone'.")
    location: str | None = Field(default=None, description="Human-readable location, e.g. 'kitchen'.")
    resource: str | None = Field(default=None, description="Stable asset identifier if known.")


class Verification(BaseModel):
    condition: str = Field(description="Declarative success condition, e.g. 'temperature_c <= 24'.")
    timeout_seconds: int = Field(default=300, ge=1)
    retry_limit: int = Field(default=0, ge=0)


class Evidence(BaseModel):
    """Provenance: which observation led to this contract."""

    observation_id: str | None = None
    summary: str | None = None


class PhysicalActionContract(BaseModel):
    """Tool-independent description of a desired physical-world outcome."""

    model_config = ConfigDict(json_schema_extra={"$id": "https://github.com/mctlhq/newton-mcp-gateway/schemas/physical-action-contract.schema.json"})

    version: str = Field(default="0.1", pattern=r"^0\.1$")
    goal: str = Field(description="Desired outcome, independent of any specific tool.")
    reason: str = Field(description="Why the action is proposed.")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    target: Target
    constraints: dict[str, Any] = Field(default_factory=dict)
    risk: Risk
    reversible: bool = True
    requires_confirmation: bool | None = Field(
        default=None, description="Explicit override; if None the policy decides from risk."
    )
    verification: Verification
    evidence: Evidence | None = None
