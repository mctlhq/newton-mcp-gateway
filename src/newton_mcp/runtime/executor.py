"""Executor: turns an authorized `CandidateAction` into one bounded MCP tool call.

A successful MCP tool call is not a successful physical action -- see
`docs/action-runtime.md`. `Executor.execute()` is the *only* code in this
package that transitions a record into `EXECUTING`: `AUTHORIZED -> EXECUTING`
for a first attempt, `FAILED -> EXECUTING` (with `verified_failure=True`) for
a retry. It re-checks the context-bound `Approval` immediately before every
attempt, the first one and any retry alike, and raises before opening any
transport if that check fails.

`run_action()`, also in this module, is the small attempt loop that
implements the retry rule: verify before ever retrying, and never re-send a
non-idempotent action whose outcome is unknown. It delegates every
`-> EXECUTING` transition to `Executor.execute()` and never transitions into
`EXECUTING` itself.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from pydantic import BaseModel, ConfigDict

from newton_mcp.action.approval import Approval, verify_approval
from newton_mcp.action.contract import PhysicalActionContract
from newton_mcp.runtime.audit import AuditSink
from newton_mcp.runtime.catalog import CapabilityCatalog, default_client_factory
from newton_mcp.runtime.config import ServerConfig
from newton_mcp.runtime.lifecycle import ActionRecord, ActionState, transition
from newton_mcp.runtime.resolver import CandidateAction

if TYPE_CHECKING:
    from newton_mcp.runtime.verifier import Verifier

DEFAULT_CALL_TIMEOUT_SECONDS = 30.0


class SupportsCallTool(Protocol):
    """The slice of `mcp.Client` the executor needs, once entered as a context manager."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


ToolClientFactory = Callable[[ServerConfig], AbstractAsyncContextManager[SupportsCallTool]]


class ExecutionOutcome(BaseModel):
    """The result of one `Executor.execute()` call attempt."""

    model_config = ConfigDict(frozen=True)

    state: ActionState  # EXECUTED | UNKNOWN
    tool_call_id: str
    detail: str
    result: Any | None = None


class ExecutorError(Exception):
    """Raised by `Executor.execute()` before any transition or transport is opened."""


class ApprovalRejected(ExecutorError):
    """The context-bound `Approval` failed `verify_approval()` for this attempt.

    Carries the failing `ApprovalCheck.reason`. Raised on every attempt --
    the first one and a retry alike -- with no transition, no audit line and
    no transport opened. `execute()` itself never distinguishes a first
    attempt from a retry here; `run_action()` decides what a rejection means:
    on the first attempt it propagates with the record left `AUTHORIZED`, on
    a retry it transitions `FAILED -> ESCALATED`.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"approval rejected: {reason}")


class Executor:
    """Calls the chosen MCP tool with a bounded timeout, guarded by an approval re-check."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        client_factory: ToolClientFactory | None = None,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        sink: AuditSink | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._catalog = catalog
        self._client_factory: ToolClientFactory = client_factory or default_client_factory  # type: ignore[assignment]
        self._call_timeout_seconds = call_timeout_seconds
        self._sink = sink
        self._id_factory = id_factory

    async def execute(
        self,
        candidate: CandidateAction,
        record: ActionRecord,
        *,
        approval: Approval,
        policy_version: str,
        now: datetime,
        verified_failure: bool = False,
    ) -> tuple[ActionRecord, ExecutionOutcome]:
        """Issue one call attempt for `candidate`, guarded by server + approval checks.

        Raises:
            ExecutorError: the candidate's server is absent from the loaded
                runtime config, or its `binding_identity` no longer matches
                the candidate's -- before any transport is opened.
            ApprovalRejected: `verify_approval()` failed for this attempt --
                before any transition, audit line or transport.
        """
        server = self._resolve_server(candidate)

        check = verify_approval(approval, candidate, record.action_id, policy_version, now)
        if not check.valid:
            raise ApprovalRejected(check.reason)

        reason = (
            f"executor retrying attempt {record.attempt + 1} after a verified failure"
            if verified_failure
            else "executor issuing the call attempt"
        )
        record = transition(
            record,
            ActionState.EXECUTING,
            reason,
            now=now,
            sink=self._sink,
            args=candidate.args,
            verified_failure=verified_failure,
            id_factory=self._id_factory,
        )

        try:
            with anyio.fail_after(self._call_timeout_seconds):
                async with self._client_factory(server) as client:
                    result = await client.call_tool(candidate.tool_name, candidate.args)
        except Exception as exc:
            record = transition(
                record,
                ActionState.UNKNOWN,
                f"call attempt {record.attempt} timed out or the transport failed: {exc!r}",
                now=now,
                sink=self._sink,
            )
            return record, ExecutionOutcome(
                state=ActionState.UNKNOWN,
                tool_call_id=record.tool_call_id,
                detail=f"timeout or transport failure: {exc!r}",
            )

        record = transition(
            record,
            ActionState.EXECUTED,
            f"call attempt {record.attempt} returned a result (does not prove the physical outcome)",
            now=now,
            sink=self._sink,
        )
        return record, ExecutionOutcome(
            state=ActionState.EXECUTED,
            tool_call_id=record.tool_call_id,
            detail="call returned a result",
            result=result,
        )

    def _resolve_server(self, candidate: CandidateAction) -> ServerConfig:
        server = next(
            (s for s in self._catalog.config.servers if s.resolved_identity == candidate.server_identity),
            None,
        )
        if server is None:
            raise ExecutorError(
                f"server {candidate.server_identity!r} is not present in the currently loaded runtime config"
            )
        if server.binding_identity != candidate.server_binding_identity:
            raise ExecutorError(
                f"server {candidate.server_identity!r} binding_identity has changed since the candidate "
                "was resolved; refusing to call a re-pointed server"
            )
        return server


async def run_action(
    candidate: CandidateAction,
    contract: PhysicalActionContract,
    record: ActionRecord,
    *,
    approval: Approval,
    policy_version: str,
    executor: Executor,
    verifier: "Verifier",
    now_fn: Callable[[], datetime],
    sink: AuditSink | None = None,
) -> tuple[ActionRecord, ActionState]:
    """The retry rule: verify before ever retrying, never re-send a non-idempotent action.

    Delegates every `-> EXECUTING` transition to `Executor.execute()`;
    `run_action()` itself only decides retry vs escalate and never
    transitions into `EXECUTING`. Always ends in exactly `SUCCEEDED` or
    `ESCALATED`.
    """
    retry = False
    while True:
        try:
            record, _execution = await executor.execute(
                candidate,
                record,
                approval=approval,
                policy_version=policy_version,
                now=now_fn(),
                verified_failure=retry,
            )
        except ApprovalRejected as exc:
            if not retry:
                # Record is still AUTHORIZED: nothing was transitioned, audited or called.
                raise
            record = transition(
                record,
                ActionState.ESCALATED,
                f"approval no longer valid for the retry: {exc.reason}",
                now=now_fn(),
                sink=sink,
            )
            return record, ActionState.ESCALATED

        record, verification = await verifier.verify(candidate, contract, record, now=now_fn())

        if verification.state in (ActionState.SUCCEEDED, ActionState.ESCALATED):
            return record, verification.state

        # verification.state is FAILED: a verified failure.
        if candidate.idempotent and record.attempt <= contract.verification.retry_limit:
            retry = True
            continue

        record = transition(
            record,
            ActionState.ESCALATED,
            (
                f"verified failure not retried: idempotent={candidate.idempotent}, "
                f"attempt={record.attempt}, retry_limit={contract.verification.retry_limit}"
            ),
            now=now_fn(),
            sink=sink,
        )
        return record, ActionState.ESCALATED
