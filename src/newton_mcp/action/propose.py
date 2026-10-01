"""`newton_propose_action`: turn an observation into exactly one validated
`PhysicalActionContract`, or fail with the raw model text and the exact
validation errors.

Backend-agnostic: driven entirely through the `NewtonBackend` protocol so it
is testable with a scripted fake. The gateway never guesses, repairs or
partially fills a contract -- it either returns one that validated, or it
returns a failure.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field, ValidationError

from newton_mcp.action.contract import Evidence, PhysicalActionContract
from newton_mcp.action.prompts import build_contract_system_prompt, build_retry_suffix
from newton_mcp.errors import InputValidationError
from newton_mcp.newton.models import DataEvent, NewtonQueryRequest
from newton_mcp.newton.protocol import NewtonBackend

MAX_ATTEMPTS = 2
SUMMARY_MAX_CHARS = 280

_PROPOSE_QUERY = "Propose exactly one Physical Action Contract for this observation."

ErrorKind = Literal[
    "backend_failed",
    "empty_output",
    "not_a_string",
    "invalid_json",
    "not_an_object",
    "validation_error",
    "goal_not_allowed",
]


class ProposeError(BaseModel):
    attempt: int
    kind: ErrorKind
    message: str
    loc: str | None = None


class ProposeActionResult(BaseModel):
    status: Literal["completed", "failed"]
    contract: PhysicalActionContract | None = None
    raw_text: str | None = None
    errors: list[ProposeError] = Field(default_factory=list)
    backend: Literal["mock", "api"]
    observation_id: str


def _normalise_observation(text_events: Sequence[str], json_events: Sequence[str]) -> None:
    """Raise `ValueError` for a blank observation or an unparseable `json_events` entry.

    No backend call is made when this raises.
    """
    has_non_empty = any(t.strip() for t in text_events) or any(j.strip() for j in json_events)
    if not has_non_empty:
        raise InputValidationError(
            "at least one of text_events or json_events must contain a non-empty entry"
        )
    for idx, entry in enumerate(json_events):
        try:
            json.loads(entry)
        except (json.JSONDecodeError, ValueError) as exc:
            raise InputValidationError(f"json_events[{idx}] is not a parseable JSON document: {exc}") from None


def _normalise_allowed_goals(allowed_goals: Sequence[str] | None) -> tuple[str, ...]:
    """Strip and de-duplicate `allowed_goals`, preserving the caller's order.

    Raises `ValueError` if `allowed_goals` was supplied but reduces to no
    non-blank entry, so an empty list can never be silently read as "any
    goal is allowed".
    """
    if allowed_goals is None:
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for goal in allowed_goals:
        stripped = goal.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        cleaned.append(stripped)
    if not cleaned:
        raise InputValidationError("allowed_goals was supplied but contains no non-blank entry")
    return tuple(cleaned)


def _observation_id(text_events: Sequence[str], json_events: Sequence[str]) -> str:
    canonical = json.dumps({"text": list(text_events), "json": list(json_events)}, sort_keys=True)
    return "obs-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _summary(text_events: Sequence[str], json_events: Sequence[str]) -> str:
    joined = " | ".join([*text_events, *json_events])
    collapsed = " ".join(joined.split())
    if len(collapsed) > SUMMARY_MAX_CHARS:
        return collapsed[: SUMMARY_MAX_CHARS - 3] + "..."
    return collapsed


def _build_events(text_events: Sequence[str], json_events: Sequence[str]) -> list[DataEvent]:
    events = [DataEvent.text(t) for t in text_events]
    events += [DataEvent(type="data.json", event_data={"contents": j}) for j in json_events]
    return events


async def propose_action(
    backend: NewtonBackend,
    *,
    model: str,
    text_events: Sequence[str] = (),
    json_events: Sequence[str] = (),
    allowed_goals: Sequence[str] | None = None,
    observation_id: str | None = None,
    max_new_tokens: int = 700,
) -> ProposeActionResult:
    _normalise_observation(text_events, json_events)
    effective_allowed_goals = _normalise_allowed_goals(allowed_goals)
    effective_observation_id = observation_id or _observation_id(text_events, json_events)

    system_prompt = build_contract_system_prompt(effective_allowed_goals)
    request = NewtonQueryRequest(
        model=model,
        query=_PROPOSE_QUERY,
        system_prompt=system_prompt,
        instruction_prompt=system_prompt,
        events=_build_events(text_events, json_events),
        max_new_tokens=max_new_tokens,
    )

    errors: list[ProposeError] = []
    last_raw_text: str | None = None
    contract: PhysicalActionContract | None = None
    backend_label: Literal["mock", "api"] = "mock"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await backend.query(request)
        backend_label = result.backend

        if result.status == "failed":
            message = result.error or "backend reported status=failed without an error message"
            errors.append(ProposeError(attempt=attempt, kind="backend_failed", message=message))
            raw_text = (
                result.outputs[0]
                if result.outputs and isinstance(result.outputs[0], str)
                else None
            )
            return ProposeActionResult(
                status="failed",
                contract=None,
                raw_text=raw_text,
                errors=errors,
                backend=backend_label,
                observation_id=effective_observation_id,
            )

        attempt_errors = _classify_output(result.outputs, effective_allowed_goals, attempt)
        if isinstance(attempt_errors, PhysicalActionContract):
            contract = attempt_errors
            break
        raw_text, new_errors = attempt_errors
        if raw_text is not None:
            last_raw_text = raw_text
        errors.extend(new_errors)

        if attempt < MAX_ATTEMPTS:
            retry_errors = [e for e in errors if e.attempt == attempt]
            suffix = build_retry_suffix(retry_errors)
            request = request.model_copy(
                update={
                    "system_prompt": system_prompt + suffix,
                    "instruction_prompt": system_prompt + suffix,
                }
            )

    if contract is not None:
        evidence = Evidence(
            observation_id=effective_observation_id,
            summary=_summary(text_events, json_events),
        )
        final_contract = contract.model_copy(update={"evidence": evidence})
        return ProposeActionResult(
            status="completed",
            contract=final_contract,
            raw_text=None,
            errors=[],
            backend=backend_label,
            observation_id=effective_observation_id,
        )

    return ProposeActionResult(
        status="failed",
        contract=None,
        raw_text=last_raw_text,
        errors=errors,
        backend=backend_label,
        observation_id=effective_observation_id,
    )


def _classify_output(
    outputs: list[Any],
    allowed_goals: tuple[str, ...],
    attempt: int,
) -> PhysicalActionContract | tuple[str | None, list[ProposeError]]:
    """Classify a `status == "completed"` attempt's output.

    Returns either the validated `PhysicalActionContract` on success, or a
    `(raw_text, errors)` pair on failure. `raw_text` is `None` when there was
    no string output to record.
    """
    if not outputs:
        return None, [ProposeError(attempt=attempt, kind="empty_output", message="backend returned an empty outputs list")]

    first = outputs[0]
    if not isinstance(first, str):
        return None, [
            ProposeError(
                attempt=attempt,
                kind="not_a_string",
                message=f"first output is {type(first).__name__}, not a string",
            )
        ]

    raw_text = first.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return raw_text, [
            ProposeError(attempt=attempt, kind="invalid_json", message=f"model output is not valid JSON: {exc}")
        ]

    if not isinstance(parsed, dict):
        return raw_text, [
            ProposeError(
                attempt=attempt,
                kind="not_an_object",
                message=f"parsed JSON is a {type(parsed).__name__}, not a JSON object",
            )
        ]

    try:
        candidate = PhysicalActionContract.model_validate(parsed)
    except ValidationError as exc:
        errors = [
            ProposeError(
                attempt=attempt,
                kind="validation_error",
                message=err["msg"],
                loc=".".join(str(p) for p in err["loc"]) or None,
            )
            for err in exc.errors()
        ]
        return raw_text, errors

    if allowed_goals and candidate.goal not in allowed_goals:
        return raw_text, [
            ProposeError(
                attempt=attempt,
                kind="goal_not_allowed",
                message=f"goal {candidate.goal!r} is not in allowed_goals {list(allowed_goals)}",
            )
        ]

    return candidate
