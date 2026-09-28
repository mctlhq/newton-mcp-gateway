"""Approval: cryptographic binding of one exact resolved action.

An `Approval` proves *which action* it covers, not *who* granted it -- a
keyless sha256 is context-binding, not authentication (see
docs/action-runtime.md). Anyone able to construct an `Approval` can compute a
valid `binding`; signed approvals and an authenticated approver are out of
scope (issue #7 territory). Lifecycle, correlation ids beyond `action_id`,
audit, revocation and execution are all out of scope here too -- this module
only proves a candidate is the one an approval was granted for.

`ResolvedCandidate` is a `typing.Protocol` (following `SupportsListTools` in
`runtime/catalog.py`) rather than an import of `runtime.resolver.CandidateAction`,
so `action/` never imports `runtime/`: `runtime/` already imports
`action/contract.py`, and a reverse import of a concrete `CandidateAction`
would make `newton_mcp.action` circular.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from newton_mcp.canonical import canonical_timestamp, sha256_hex


class ResolvedCandidate(Protocol):
    """The slice of `runtime.resolver.CandidateAction` an approval binds to."""

    server_identity: str
    server_binding_identity: str
    tool_name: str
    args: dict[str, Any]


class Approval(BaseModel):
    """An approval bound to one exact action via `binding`. See `compute_binding`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str
    action_id: str
    server_identity: str
    tool_name: str
    args_digest: str
    policy_version: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    binding: str


class ApprovalCheck(BaseModel):
    """The result of `verify_approval`. `reason` never echoes an `args` value."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    reason: str


def compute_binding(
    *,
    server_identity: str,
    tool_name: str,
    args: dict[str, Any],
    action_id: str,
    policy_version: str,
    expires_at: datetime,
) -> str:
    """sha256 over the canonical JSON of exactly these six fields, nothing else.

    `args` enters in full -- not `args_digest` -- so the binding payload is
    self-contained: verifying it never depends on trusting that some other
    digest was computed the same way on both sides. This is the single place
    the payload is assembled; `create_approval` and `verify_approval` both
    call through it rather than building the dict themselves.
    """
    payload = {
        "server_identity": server_identity,
        "tool_name": tool_name,
        "args": args,
        "action_id": action_id,
        "policy_version": policy_version,
        "expires_at": canonical_timestamp(expires_at),
    }
    return sha256_hex(payload)


def create_approval(
    candidate: ResolvedCandidate,
    *,
    action_id: str,
    policy_version: str,
    approved_by: str,
    approved_at: datetime,
    expires_at: datetime,
    approval_id: str,
) -> Approval:
    """Build an `Approval` bound to `candidate` via `compute_binding`.

    `server_identity` on the resulting approval is
    `candidate.server_binding_identity` -- the composite configured-label +
    transport-fingerprint value -- never the bare configured label, so
    re-pointing the server's transport under an unchanged label invalidates
    this approval. See docs/action-runtime.md.
    """
    binding = compute_binding(
        server_identity=candidate.server_binding_identity,
        tool_name=candidate.tool_name,
        args=candidate.args,
        action_id=action_id,
        policy_version=policy_version,
        expires_at=expires_at,
    )
    return Approval(
        approval_id=approval_id,
        action_id=action_id,
        server_identity=candidate.server_binding_identity,
        tool_name=candidate.tool_name,
        args_digest=sha256_hex(candidate.args),
        policy_version=policy_version,
        approved_by=approved_by,
        approved_at=approved_at,
        expires_at=expires_at,
        binding=binding,
    )


def verify_approval(
    approval: Approval,
    candidate: ResolvedCandidate,
    action_id: str,
    policy_version: str,
    now: datetime,
) -> ApprovalCheck:
    """Check `approval` against `candidate`, `action_id`, `policy_version` and `now`.

    Raises:
        ValueError: `now` is naive -- a programming error, not an invalid
            approval, so it is never reported back as `valid=False`.

    Checks run in a fixed order and the first failure's reason is returned,
    naming the field but never an `args` value: expiry, `server_identity`,
    `tool_name`, `args_digest`, `action_id`, `policy_version`, then the
    binding recomputed from `candidate` and `approval.expires_at`, compared
    with `hmac.compare_digest`. The field-by-field checks exist only to give
    an actionable reason; the binding recompute is authoritative -- it is
    what catches a mutated `expires_at`, since the recomputed payload always
    uses `approval.expires_at`, so editing that field changes the recomputed
    digest while the stored `binding` does not follow.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("verify_approval: now must be timezone-aware")

    if now >= approval.expires_at:
        return ApprovalCheck(valid=False, reason="approval has expired")

    if approval.server_identity != candidate.server_binding_identity:
        return ApprovalCheck(valid=False, reason="server_identity does not match the resolved candidate")

    if approval.tool_name != candidate.tool_name:
        return ApprovalCheck(valid=False, reason="tool_name does not match the resolved candidate")

    if approval.args_digest != sha256_hex(candidate.args):
        return ApprovalCheck(valid=False, reason="args do not match the resolved candidate")

    if approval.action_id != action_id:
        return ApprovalCheck(valid=False, reason="action_id does not match")

    if approval.policy_version != policy_version:
        return ApprovalCheck(valid=False, reason="policy_version does not match")

    recomputed = compute_binding(
        server_identity=candidate.server_binding_identity,
        tool_name=candidate.tool_name,
        args=candidate.args,
        action_id=action_id,
        policy_version=policy_version,
        expires_at=approval.expires_at,
    )
    if not hmac.compare_digest(recomputed, approval.binding):
        return ApprovalCheck(valid=False, reason="binding does not match the recomputed digest")

    return ApprovalCheck(valid=True, reason="binding verified")
