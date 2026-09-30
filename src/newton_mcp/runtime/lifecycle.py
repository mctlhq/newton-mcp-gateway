"""Explicit action-lifecycle state machine, correlation ids, and guarded transitions.

The central safety property: the runtime must be able to say "I do not know
whether this happened". A tool-call timeout or transport failure is
`UNKNOWN`, never success, and `UNKNOWN` may never go straight back to
`EXECUTING` -- blindly re-issuing a non-idempotent physical action is exactly
the failure mode this module exists to prevent. `PROPOSED` also cannot reach
`EXECUTING` directly: execution requires passing through authorization.

The safety argument lives in one readable data structure, `ALLOWED_TRANSITIONS`,
not in control flow -- a reviewer audits the two absent edges above by reading
one mapping, not by tracing branches. `transition()` is the single mutation
point; it consults nothing else.

This module executes nothing: no `call_tool`, no MCP session, no timeout
policy, no verification logic, and no retry *decision* (only that a retry is
*possible* and what it must carry). The executor and verifier (#8) consume
`transition()`; they are not part of it. See docs/action-runtime.md and the
owner amendments recorded in requirements.md.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict

from newton_mcp.canonical import canonical_timestamp, sha256_hex
from newton_mcp.runtime.audit import AuditEvent, AuditSink, redact_args


class ActionState(StrEnum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXECUTING = "executing"
    EXECUTED = "executed"
    UNKNOWN = "unknown"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ESCALATED = "escalated"


#: The only source of truth `transition()` consults for legality. Read-only at
#: runtime via `MappingProxyType`, so an importer cannot widen it in place --
#: the same fail-loudly instinct as `extra="forbid"` in `runtime/config.py`.
ALLOWED_TRANSITIONS: Mapping[ActionState, frozenset[ActionState]] = MappingProxyType(
    {
        ActionState.PROPOSED: frozenset({ActionState.AUTHORIZED, ActionState.DENIED}),
        ActionState.AUTHORIZED: frozenset({ActionState.EXECUTING}),
        # EXECUTING deliberately omits FAILED (owner amendment): a synchronous
        # MCP error response is still a completed call attempt and does not
        # prove the physical action did not (partly) happen, so it goes to
        # EXECUTED and then VERIFYING. FAILED is reachable only from VERIFYING,
        # so a FAILED record always means a *verified* failure.
        ActionState.EXECUTING: frozenset({ActionState.EXECUTED, ActionState.UNKNOWN}),
        ActionState.EXECUTED: frozenset({ActionState.VERIFYING}),
        # UNKNOWN deliberately omits EXECUTING: an unobserved outcome must be
        # verified or escalated, never blindly re-attempted -- this is the
        # safety property the issue exists to enforce.
        ActionState.UNKNOWN: frozenset({ActionState.VERIFYING, ActionState.ESCALATED}),
        ActionState.VERIFYING: frozenset(
            {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.ESCALATED}
        ),
        ActionState.FAILED: frozenset({ActionState.EXECUTING, ActionState.ESCALATED}),
        ActionState.DENIED: frozenset(),
        ActionState.SUCCEEDED: frozenset(),
        ActionState.ESCALATED: frozenset(),
    }
)

#: Transitions that require an explicit `verified_failure=True` on `transition()`.
REQUIRES_VERIFIED_FAILURE: frozenset[tuple[ActionState, ActionState]] = frozenset(
    {(ActionState.FAILED, ActionState.EXECUTING)}
)

assert set(ALLOWED_TRANSITIONS) == set(ActionState), (
    "ALLOWED_TRANSITIONS must cover every ActionState member, so adding a state "
    "without deciding its outgoing edges fails immediately instead of producing "
    "a silently terminal state"
)


class IllegalTransition(ValueError):
    """Raised by `transition()` for any `(from_state, new_state)` the table does not allow.

    Carries `from_state`, `to_state` and `allowed` so a caller can branch on
    the exception without parsing its message, following `TemplateError` in
    `runtime/resolver.py`.
    """

    def __init__(
        self,
        from_state: ActionState,
        to_state: ActionState,
        allowed: frozenset[ActionState],
        message: str | None = None,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.allowed = allowed
        if message is None:
            if allowed:
                message = (
                    f"illegal transition {from_state} -> {to_state}: allowed targets from "
                    f"{from_state} are {sorted(allowed)}"
                )
            else:
                message = f"illegal transition {from_state} -> {to_state}: {from_state} is a terminal state"
        super().__init__(message)


_ID_PREFIXES = {
    "observation_id": "obs",
    "action_id": "act",
    "tool_call_id": "call",
    "verification_id": "ver",
}

#: Ids that only a retry `FAILED -> EXECUTING` may supply/replace on `transition()`.
_ATTEMPT_SCOPED_IDS = ("tool_call_id", "verification_id")
#: Ids that are fixed at `new_action_record()` time and never accepted by `transition()`.
_CORRELATION_ROOT_IDS = ("observation_id", "action_id")


def _default_id_factory() -> str:
    """16 hex characters, echoing `action/propose.py`'s `obs-<16 hex>` shape."""
    return secrets.token_hex(8)


def _make_id(kind: str, supplied: str | None, id_factory: Callable[[], str]) -> str:
    if supplied is not None:
        return supplied
    return f"{_ID_PREFIXES[kind]}-{id_factory()}"


class ActionRecord(BaseModel):
    """One physical action's current lifecycle state and its four correlation ids.

    Immutable: `transition()` returns a new record and never mutates the one
    it was given. `observation_id`/`action_id` are fixed for the whole action;
    `tool_call_id`/`verification_id` are attempt-scoped (see `transition()`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    action_id: str
    tool_call_id: str
    verification_id: str
    state: ActionState
    attempt: int
    created_at: datetime
    updated_at: datetime


def new_action_record(
    *,
    now: datetime,
    observation_id: str | None = None,
    action_id: str | None = None,
    tool_call_id: str | None = None,
    verification_id: str | None = None,
    id_factory: Callable[[], str] | None = None,
) -> ActionRecord:
    """Create a fresh `ActionRecord` in `PROPOSED`, attempt `0`, all four ids populated.

    Any id the caller omits is generated as `f"{prefix}-{id_factory()}"`
    (`obs-`/`act-`/`call-`/`ver-`), with a default `id_factory =
    secrets.token_hex(8)` (16 hex characters). `id_factory` is the injectable
    seam that makes ids deterministic in tests, exactly like `ClientFactory`
    in `runtime/catalog.py`.

    Callers normally pass `observation_id` through from the
    `newton_propose_action` result (its deterministic `obs-<sha256>`);
    generating one here is a fallback only.

    The creation-time `tool_call_id`/`verification_id` are attempt 1's ids --
    not placeholders -- and stay unchanged through attempt 1's `EXECUTING`
    and `VERIFYING` (owner amendment); see `transition()`.

    Raises:
        ValueError: `now` is naive (no usable `tzinfo`).
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("new_action_record: now must be timezone-aware")

    factory = id_factory or _default_id_factory
    return ActionRecord(
        observation_id=_make_id("observation_id", observation_id, factory),
        action_id=_make_id("action_id", action_id, factory),
        tool_call_id=_make_id("tool_call_id", tool_call_id, factory),
        verification_id=_make_id("verification_id", verification_id, factory),
        state=ActionState.PROPOSED,
        attempt=0,
        created_at=now,
        updated_at=now,
    )


def transition(
    record: ActionRecord,
    new_state: ActionState,
    reason: str,
    *,
    now: datetime,
    sink: AuditSink | None = None,
    verified_failure: bool = False,
    args: dict[str, Any] | None = None,
    id_factory: Callable[[], str] | None = None,
    **ids: str,
) -> ActionRecord:
    """Move `record` from its current state to `new_state`, returning a new record.

    Checks run in this order, each failing before anything is written:

    1. `reason.strip()` must be non-empty.
    2. `**ids` keys are validated: `observation_id`/`action_id` always raise
       (they are correlation roots, fixed at record creation);
       `tool_call_id`/`verification_id` are accepted only on a retry
       (`record.state is FAILED and new_state is EXECUTING`), where they
       override the generated pair -- on every other transition, including
       `AUTHORIZED -> EXECUTING` and any entry into `VERIFYING`, either key
       raises; any other keyword raises as unknown.
    3. `new_state` must be in `ALLOWED_TRANSITIONS[record.state]`, else
       `IllegalTransition`.
    4. `(record.state, new_state) in REQUIRES_VERIFIED_FAILURE` implies
       `verified_failure is True`, else `IllegalTransition`.
    5. `now` must be timezone-aware.

    `attempt` increments by one only when `new_state is EXECUTING` (so the
    first execution is attempt 1, a retry after `FAILED` is attempt 2).
    `tool_call_id`/`verification_id` are attempt-scoped: only a retry
    `FAILED -> EXECUTING` replaces **both** together (each supplied value, or
    a freshly minted one); every other transition -- including
    `AUTHORIZED -> EXECUTING` and `EXECUTED/UNKNOWN -> VERIFYING` -- leaves
    both unchanged.

    If `sink is not None`, exactly one `AuditEvent` is written after the new
    record has been computed, so the line always reflects a transition that
    actually happened; a rejected transition writes nothing. When `args` is
    supplied the event carries a redacted `args` mapping plus
    `args_digest = newton_mcp.canonical.sha256_hex(args)` computed over the
    *unredacted* args, so the digest is byte-identical to
    `Approval.args_digest` for the same resolved action.

    Raises:
        ValueError: a blank `reason`, a forbidden or unknown `**ids` keyword,
            or a naive `now`.
        IllegalTransition: `new_state` is not reachable from `record.state`,
            or a `FAILED -> EXECUTING` retry is requested without
            `verified_failure=True`.
    """
    if not reason.strip():
        raise ValueError("transition: reason must not be blank")

    is_retry = record.state is ActionState.FAILED and new_state is ActionState.EXECUTING

    for key in ids:
        if key in _CORRELATION_ROOT_IDS:
            raise ValueError(
                f"transition: {key} is a correlation root fixed at new_action_record() time; "
                "it cannot be supplied to transition()"
            )
        if key in _ATTEMPT_SCOPED_IDS:
            if not is_retry:
                raise ValueError(
                    f"transition: {key} can only be supplied on a retry FAILED -> EXECUTING "
                    f"transition, not on {record.state} -> {new_state}"
                )
            continue
        raise ValueError(f"transition: unknown id keyword {key!r}")

    allowed = ALLOWED_TRANSITIONS.get(record.state, frozenset())
    if new_state not in allowed:
        raise IllegalTransition(record.state, new_state, allowed)

    if (record.state, new_state) in REQUIRES_VERIFIED_FAILURE and not verified_failure:
        raise IllegalTransition(
            record.state,
            new_state,
            allowed,
            message=(
                f"illegal transition {record.state} -> {new_state}: a retry requires a verified "
                "failure; pass verified_failure=True"
            ),
        )

    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("transition: now must be timezone-aware")

    factory = id_factory or _default_id_factory
    attempt = record.attempt + 1 if new_state is ActionState.EXECUTING else record.attempt

    if is_retry:
        tool_call_id = _make_id("tool_call_id", ids.get("tool_call_id"), factory)
        verification_id = _make_id("verification_id", ids.get("verification_id"), factory)
    else:
        tool_call_id = record.tool_call_id
        verification_id = record.verification_id

    new_record = record.model_copy(
        update={
            "state": new_state,
            "attempt": attempt,
            "updated_at": now,
            "tool_call_id": tool_call_id,
            "verification_id": verification_id,
        }
    )

    if sink is not None:
        event_fields: dict[str, Any] = {
            "observation_id": new_record.observation_id,
            "action_id": new_record.action_id,
            "tool_call_id": new_record.tool_call_id,
            "verification_id": new_record.verification_id,
            "from_state": record.state.value,
            "to_state": new_record.state.value,
            "reason": reason,
            "attempt": new_record.attempt,
            "verified_failure": verified_failure,
            "at": canonical_timestamp(now),
        }
        if args is not None:
            event_fields["args"] = redact_args(args)
            event_fields["args_digest"] = sha256_hex(args)
        sink.write(AuditEvent(**event_fields))

    return new_record
