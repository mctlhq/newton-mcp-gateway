"""Verifier: re-observes the world through a capability's `read_tool`.

A completed tool call attempt does not prove a physical outcome happened;
the verifier is the piece that actually looks. It polls only the
capability's `read_tool` -- never the action tool -- evaluates the
contract's structured `verification.condition`
(`newton_mcp.action.conditions.evaluate`) against each observation, and
ends in exactly one of `SUCCEEDED`, `FAILED` (a *verified* failure, only
reachable with a known negative result from the latest poll) or `ESCALATED`
(the latest poll could not establish the outcome -- calling that a verified
failure would license a retry on no evidence).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict

from newton_mcp.action.conditions import evaluate
from newton_mcp.action.contract import PhysicalActionContract
from newton_mcp.runtime.audit import AuditSink
from newton_mcp.runtime.catalog import CapabilityCatalog, CatalogEntry, default_client_factory
from newton_mcp.runtime.config import ServerConfig
from newton_mcp.runtime.executor import ToolClientFactory
from newton_mcp.runtime.lifecycle import ActionRecord, ActionState, transition
from newton_mcp.runtime.resolver import CandidateAction

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0

Clock = Callable[[], float]
Sleep = Callable[[float], "Any"]


class VerificationOutcome(BaseModel):
    """The result of one `Verifier.verify()` run."""

    model_config = ConfigDict(frozen=True)

    state: ActionState  # SUCCEEDED | FAILED | ESCALATED
    observations: int
    reason: str


def observation_from_result(result: Any) -> Mapping[str, Any] | None:
    """Turn a `read_tool` call's MCP result into an observation mapping, or `None` (a failed poll).

    An error result (`is_error`) is never an observation, even when it
    carries structured content. Otherwise: `structured_content` when it is a
    mapping, else a single text content block parsed as JSON into an object,
    else no observation -- a non-JSON or non-object text result never counts
    as an observation (default chosen for requirements.md open question 4).
    """
    if getattr(result, "is_error", False):
        return None

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured if isinstance(structured, Mapping) else None

    content = getattr(result, "content", None) or []
    if len(content) != 1:
        return None
    text = getattr(content[0], "text", None)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class Verifier:
    """Polls a capability's `read_tool` and evaluates the contract's condition."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        client_factory: ToolClientFactory | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        sink: AuditSink | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._catalog = catalog
        self._client_factory: ToolClientFactory = client_factory or default_client_factory  # type: ignore[assignment]
        self._poll_interval_seconds = poll_interval_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._sink = sink
        self._clock: Clock = clock or anyio.current_time
        self._sleep: Sleep = sleep or anyio.sleep
        self._id_factory = id_factory

    @property
    def sink(self) -> AuditSink | None:
        """The audit sink every transition this verifier makes is written to."""
        return self._sink

    async def verify(
        self,
        candidate: CandidateAction,
        contract: PhysicalActionContract,
        record: ActionRecord,
        *,
        now: datetime,
    ) -> tuple[ActionRecord, VerificationOutcome]:
        candidate = candidate.model_copy(deep=True)
        contract = contract.model_copy(deep=True)
        record = transition(
            record,
            ActionState.VERIFYING,
            "verifier beginning observation",
            now=now,
            sink=self._sink,
            id_factory=self._id_factory,
        )

        entry = self._find_entry(candidate)
        unverifiable_reason = self._unverifiable_reason(candidate, entry)
        if unverifiable_reason is not None:
            record = transition(
                record, ActionState.ESCALATED, unverifiable_reason, now=now, sink=self._sink,
                id_factory=self._id_factory,
            )
            return record, VerificationOutcome(state=ActionState.ESCALATED, observations=0, reason=unverifiable_reason)

        assert entry is not None and entry.read_tool is not None  # narrowed by _unverifiable_reason
        hint_note = ""
        if entry.read_tool.read_only_hint is None:
            hint_note = " (read_tool has no read_only_hint annotation; allowed)"

        server = entry.server.model_copy(deep=True)
        deadline = self._clock() + contract.verification.timeout_seconds
        observations = 0
        latest_known = False

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break

            observation = await self._poll(server, candidate, timeout_seconds=remaining)
            # A transport may finish after cancellation or an injected clock
            # may advance during the read: late evidence cannot prove an
            # outcome within the contract's verification window.
            if self._clock() >= deadline:
                latest_known = False
                break

            latest_known = False
            if observation is not None:
                observations += 1
                result = evaluate(contract.verification.condition, observation)
                latest_known = result.known
                if result.known and result.satisfied:
                    reason = f"{result.reason}{hint_note}"
                    record = transition(
                        record, ActionState.SUCCEEDED, reason, now=now, sink=self._sink,
                        id_factory=self._id_factory,
                    )
                    return record, VerificationOutcome(
                        state=ActionState.SUCCEEDED, observations=observations, reason=reason
                    )

            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            await self._sleep(min(self._poll_interval_seconds, remaining))

        if not latest_known:
            reason = f"latest poll did not establish the outcome before the deadline{hint_note}"
            record = transition(
                record, ActionState.ESCALATED, reason, now=now, sink=self._sink, id_factory=self._id_factory,
            )
            return record, VerificationOutcome(state=ActionState.ESCALATED, observations=observations, reason=reason)

        reason = f"condition never satisfied by the deadline after {observations} observation(s){hint_note}"
        record = transition(
            record, ActionState.FAILED, reason, now=now, sink=self._sink, id_factory=self._id_factory,
        )
        return record, VerificationOutcome(state=ActionState.FAILED, observations=observations, reason=reason)

    async def _poll(
        self, server: ServerConfig, candidate: CandidateAction, *, timeout_seconds: float,
    ) -> Mapping[str, Any] | None:
        assert candidate.read_tool is not None
        try:
            with anyio.fail_after(min(self._read_timeout_seconds, timeout_seconds)):
                async with self._client_factory(server) as client:
                    raw_result = await client.call_tool(candidate.read_tool, candidate.read_args)
        except Exception:
            return None
        return observation_from_result(raw_result)

    def _find_entry(self, candidate: CandidateAction) -> CatalogEntry | None:
        return next(
            (
                entry
                for entry in self._catalog.snapshot.entries
                if entry.server.resolved_identity == candidate.server_identity
                and entry.capability.tool == candidate.tool_name
            ),
            None,
        )

    @staticmethod
    def _unverifiable_reason(candidate: CandidateAction, entry: CatalogEntry | None) -> str | None:
        if candidate.read_tool is None:
            return "capability declares no read_tool; the outcome can never be verified"
        if entry is None or entry.read_tool is None:
            return f"read_tool {candidate.read_tool!r} was not discovered; cannot verify"
        if entry.server.binding_identity != candidate.server_binding_identity:
            # The executor refuses a re-pointed server before calling; the
            # verifier must refuse it too, or a catalog refreshed between
            # execution and verification would confirm the action against a
            # different server's physical state (owner review of #8).
            return (
                f"server {candidate.server_identity!r} binding_identity has changed since the "
                "candidate was resolved; refusing to verify against a re-pointed server"
            )
        if entry.read_tool.read_only_hint is False:
            return (
                f"read_tool {candidate.read_tool!r} declares read_only_hint=False; "
                "refusing to call a non-read-only tool for verification"
            )
        return None
