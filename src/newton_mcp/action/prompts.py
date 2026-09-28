"""Strict-JSON system prompt for `newton_propose_action`.

Pure, side-effect-free prompt construction: no I/O, no backend import. The
schema is derived from `PhysicalActionContract.model_json_schema()` at call
time so the prompt can never drift from the model it is asking Newton to
produce.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Sequence

from newton_mcp.action.contract import PhysicalActionContract

if TYPE_CHECKING:
    from newton_mcp.action.propose import ProposeError

CONTRACT_PROMPT_MARKER = "physical-action-contract/v0.1/strict-json"

_SAFETY_BOUND = (
    "Safety bound: only propose benign, reversible demo actions -- for example "
    "adjusting lights, HVAC within safe bounds, speaker announcements, or other "
    "benign reversible routines. Never propose locks, ovens, alarms, industrial "
    "start/stop, or safety systems."
)

_FORMAT_INSTRUCTION = (
    "Respond with exactly one JSON object that validates against the JSON schema "
    "below. No prose, no commentary, no markdown fences -- output only the raw "
    "JSON object."
)


def build_contract_system_prompt(allowed_goals: tuple[str, ...] = ()) -> str:
    """Build the strict-JSON system prompt for a contract proposal.

    Embeds the `CONTRACT_PROMPT_MARKER`, the "exactly one JSON object, no
    prose, no markdown fences" instruction, the epic's safety bound, the
    `allowed_goals` restriction (only when non-empty -- an empty tuple omits
    the section entirely rather than inventing a default goal list), and the
    verbatim `PhysicalActionContract` JSON schema.
    """
    schema_json = json.dumps(PhysicalActionContract.model_json_schema(), indent=2, sort_keys=True)
    lines = [
        CONTRACT_PROMPT_MARKER,
        "",
        "You are proposing exactly one Physical Action Contract for a physical-world "
        "observation. You are proposing an action, not performing or authorising one.",
        "",
        _FORMAT_INSTRUCTION,
        "",
        _SAFETY_BOUND,
    ]
    if allowed_goals:
        lines += [
            "",
            "The `goal` field must be exactly one of the following allowed goals:",
            *(f"- {goal}" for goal in allowed_goals),
        ]
    lines += ["", "JSON schema for the contract:", schema_json]
    return "\n".join(lines)


def build_retry_suffix(errors: Sequence["ProposeError"]) -> str:
    """Build the text appended to the system/instruction prompt for the retry attempt.

    Lists every recorded error from the failed attempt so the model can
    correct exactly what it got wrong.
    """
    lines = [
        "",
        "Your previous attempt did not produce a valid Physical Action Contract. "
        "Fix the following problems and respond again with exactly one JSON object "
        "-- no prose, no markdown fences:",
    ]
    for error in errors:
        loc = f" ({error.loc})" if error.loc else ""
        lines.append(f"- {error.kind}{loc}: {error.message}")
    return "\n".join(lines)
