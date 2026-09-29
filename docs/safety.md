# Safety: defaults, invariants, and what is deliberately not done

> This document describes the safety argument for `src/newton_mcp/runtime/` and
> `src/newton_mcp/action/policy.py` / `approval.py` / `conditions.py`, **this project's
> experimental proposal** for Direction A (MCP as Newton's action boundary). It is not an
> Archetype standard, and nothing described here has been run against a live Newton account or a
> live MCP actuator server -- every result in this document and its tests is **mock-validated**
> only, using in-process fakes (no subprocess, no socket, no credentials). See
> `docs/action-runtime.md` for how each of these pieces works; this document collects the safety
> defaults, the invariants a reviewer can check, and the known limitations in one place.

## Safety defaults: action classes

The table below is the canonical home for this repository's action-class-to-decision defaults.
It is not duplicated anywhere else in this repository; `docs/architecture.md` points here instead
of restating it. Each allowed row is cross-referenced to the actual rule in
`examples/policy.example.yaml` and `examples/smart-home/policy.yaml` that realises it -- not to
prose -- so a reviewer can check the claim against a committed file, not a promise.

| Action class | Decision | Realised by |
|---|---|---|
| HVAC within a configured comfort band (e.g. `target_temperature_c` 20-25 C) | `auto` | `hvac-within-comfort-band` (`examples/policy.example.yaml`); `ac-reduce-temperature` / `ac-raise-temperature` (`examples/smart-home/policy.yaml`), each with `arg_ranges: {target_temperature_c: {min: 20, max: 25}}` |
| Lights: on/off, or brightness within a configured band | `auto` | `lighting` (`examples/policy.example.yaml`); `lighting-on-off` / `light-brightness` (`examples/smart-home/policy.yaml`, `arg_ranges: {brightness_pct: {min: 0, max: 100}}`) |
| Speaker announcements | `confirm` | `announcements` (`examples/policy.example.yaml`); `speaker-announcement` (`examples/smart-home/policy.yaml`). Neither example auto-approves an announcement, and `announce` has no `read_tool` in either `runtime.yaml`, so it can never be verified even after a human confirms it (see Known limitations, below). |
| Unlock a door | `deny` | No rule in any policy file in this repository grants it; it falls through to `default: deny`, the only decision every committed policy file reaches for an ungranted class. |
| Start or stop industrial machinery | `deny` | Same as above: no rule exists; `default: deny` applies. |
| Disable a safety system | `deny` | Same as above: no rule exists; `default: deny` applies. |
| Any `critical`-risk action, regardless of class | `deny` | `Policy.evaluate()` denies `contract.risk is Risk.CRITICAL` before any rule in the file is even consulted (`src/newton_mcp/action/policy.py`) -- a policy file cannot override this by adding a rule. |

Locks, ovens, alarms, industrial start/stop and safety-system tools are never present in
`examples/runtime.example.yaml` or `examples/smart-home/runtime.yaml` -- there is no allow-listed
tool for the resolver to even offer a candidate for, so the `deny` rows above are enforced twice
over: once by the absence of any capability that could resolve to one, and once by `default: deny`
if a capability for one were ever added without a matching rule.

## Safety defaults: numeric

Every other safety-relevant default in this runtime is a timeout, a retry ceiling, or a bound --
collected here so a reviewer checking "what happens if this deadline is hit" has one table to
read, not five modules.

| Default | Value | Defined in |
|---|---|---|
| `Verification.timeout_seconds` | `300` | `src/newton_mcp/action/contract.py` (`Verification`) |
| `Verification.retry_limit` | `0` | `src/newton_mcp/action/contract.py` (`Verification`) -- with the default, the first verified failure escalates; no retry_limit means no committed contract retries unless it says so explicitly. |
| `DEFAULT_SERVER_TIMEOUT_SECONDS` | `10.0` | `src/newton_mcp/runtime/catalog.py` -- bounds one server's whole discovery cycle (connect, handshake, every `list_tools` page). |
| `DEFAULT_CALL_TIMEOUT_SECONDS` | `30.0` | `src/newton_mcp/runtime/executor.py` -- bounds one tool-call attempt (connect, handshake, the call). |
| `DEFAULT_POLL_INTERVAL_SECONDS` | `5.0` | `src/newton_mcp/runtime/verifier.py` -- the wait between verifier polls. |
| `DEFAULT_READ_TIMEOUT_SECONDS` | `10.0` | `src/newton_mcp/runtime/verifier.py` -- bounds one poll of the `read_tool`; also the documented worst-case deadline overrun (see Invariants, below). |
| `MAX_CONDITION_DEPTH` | `8` | `src/newton_mcp/action/conditions.py` -- caps `all`/`any` nesting depth on a `Condition`. |
| `MAX_TOOL_PAGES` | `1000` | `src/newton_mcp/runtime/catalog.py` -- caps `list_tools` pagination per server. |
| `_MAX_VALUE_CHARS` | `500` | `src/newton_mcp/runtime/audit.py` -- truncates a surviving (non-redacted) string value before it is written to the audit log. |
| `PolicyRule.max_risk` | `low` (`Risk.LOW`) | `src/newton_mcp/action/policy.py` -- a rule with no explicit `max_risk` only matches contracts at `read_only` or `low` risk. |
| `PolicyRule.min_confidence` | `0.0` | `src/newton_mcp/action/policy.py` -- a rule with no explicit floor accepts any confidence, including `None`. |
| `Policy.default` | `deny` (`Decision.DENY`) | `src/newton_mcp/action/policy.py` -- the decision when no rule in the file matches. |
| `CapabilityConfig.idempotent` | `false` | `src/newton_mcp/runtime/config.py` -- a capability must opt in to `idempotent: true` before `run_action()`'s retry rule will ever retry it. |
| `PhysicalActionContract.reversible` | `true` | `src/newton_mcp/action/contract.py` |

## Invariants

Each invariant below is a checkable claim: the code location that enforces it, and the failure it
is meant to prevent.

- **`critical` risk denies before any rule is consulted.** `Policy.evaluate()`'s first branch
  returns `deny` for `contract.risk is Risk.CRITICAL` without reading a single `PolicyRule`
  (`src/newton_mcp/action/policy.py`). Prevents a permissive or misconfigured policy file from ever
  auto-approving the highest-risk class.
- **The confirmation ceiling never grants more authority than the operator's rules.**
  `contract.requires_confirmation` can only raise an `auto` decision to `confirm`; it never
  downgrades `confirm` or `deny` (`Policy._apply_confirmation_ceiling()`). A model-authored contract
  flag can never talk its way past what the operator's policy file already denied.
- **An `arg_ranges` check that fails denies immediately.** A missing argument, a non-numeric value
  (a `bool` never counts as numeric), or a value outside its inclusive bound returns `deny` at that
  rule and does not fall through to a later, broader rule that might have auto-approved it
  (`Policy._match_rule()`). Prevents a value this rule explicitly forbids from being auto-approved
  by a less specific rule further down the file.
- **An approval binds exactly one action and is re-checked on every attempt.** The bound payload is
  `{server_identity, tool_name, args, action_id, policy_version, expires_at}`
  (`src/newton_mcp/action/approval.py`), and `Executor.execute()` calls `verify_approval()` before
  every attempt -- the first one and any retry alike (`src/newton_mcp/runtime/executor.py`, step 2).
  Prevents a retry from running on an approval that has since expired or no longer matches the
  resolved action.
- **A re-pointed transport invalidates every outstanding approval.**
  `ServerConfig.binding_identity` folds a sha256 of the server's declared transport into the
  identity an approval binds to (`src/newton_mcp/runtime/config.py`); the executor and the verifier
  each independently refuse to act when the currently loaded server's `binding_identity` no longer
  matches the candidate's (`Executor._resolve_server()`, `Verifier._unverifiable_reason()`).
  Prevents an approval granted for one server from being spent against a different server
  substituted under the same configured name.
- **A timeout yields `UNKNOWN`, never a silent success or failure.** `ALLOWED_TRANSITIONS` has no
  `UNKNOWN -> EXECUTING` edge (`src/newton_mcp/runtime/lifecycle.py`); an `UNKNOWN` outcome must
  pass through the verifier and land on `VERIFYING -> SUCCEEDED | FAILED | ESCALATED`. Prevents a
  non-idempotent physical action from being blindly re-issued while its outcome is unobserved.
- **A synchronous MCP error is a completed attempt, not proof of failure, and still gets
  verified.** There is no `EXECUTING -> FAILED` edge; any returned result (including an MCP error
  result) transitions `EXECUTING -> EXECUTED -> VERIFYING` (`Executor.execute()`,
  `ALLOWED_TRANSITIONS`). Prevents treating a tool-level error response as proof the physical
  action did not happen, when it may have partially happened.
- **`FAILED` always means a verified failure, and its retry edge demands proof.** `FAILED` is
  reachable only from `VERIFYING` (`ALLOWED_TRANSITIONS`), and the `FAILED -> EXECUTING` retry edge
  additionally requires an explicit `verified_failure=True` keyword on `transition()`, never
  inferred from a free-text reason string (`REQUIRES_VERIFIED_FAILURE`,
  `src/newton_mcp/runtime/lifecycle.py`). Prevents a record from being retried on the basis of an
  unverified claim of failure.
- **A retry additionally requires the capability to be idempotent.** `run_action()` only retries a
  verified `FAILED` when `candidate.idempotent and record.attempt <=
  contract.verification.retry_limit` (`src/newton_mcp/runtime/executor.py`); otherwise it escalates
  with no further tool call. Prevents a non-idempotent action (e.g. `announce`) from ever being
  re-sent.
- **Zero observations escalate; they are never reported as a verified failure.** The verifier
  distinguishes "the deadline passed with at least one observation, never satisfied" (`FAILED`)
  from "the deadline passed with zero observations" (`ESCALATED`) (`Verifier.verify()`). Prevents
  an unobservable world -- which proves nothing -- from licensing a retry on no evidence at all.
- **The verifier refuses to call a tool the server marks as not read-only.** `read_only_hint is
  False` on the discovered `read_tool` always escalates without a call
  (`Verifier._unverifiable_reason()`); an unannotated (`None`) hint is allowed and the fact is
  recorded on the eventual transition's reason. Prevents the verification step itself from becoming
  a second, unreviewed actuator call.
- **The verifier calls `read_tool` with `read_args`, never the action's own `args`.**
  `Verifier._poll()` passes `candidate.read_args`, rendered independently from `capability.arguments`
  (`src/newton_mcp/runtime/resolver.py`). Prevents an actuator parameter (e.g.
  `target_temperature_c`) from leaking into a call to a read-only tool.

## Demo-safe actions

Every example and test in this repository restricts itself to these classes:

- **Lights** -- on/off and brightness within a configured band.
- **HVAC within configured bounds** -- a comfort-band temperature target, never an unbounded
  setpoint.
- **Speaker announcements** -- benign, and always `confirm`, never `auto` (see the action-class
  table above).
- **Benign routines composed only of the above** -- there is no "scene" or "routine" abstraction in
  this runtime yet; a routine is whatever sequence of the above an operator's own automation issues
  as separate contracts.

The following classes are **never permitted** in any example, test, or document in this
repository, and no allow-listed tool for any of them exists in `examples/runtime.example.yaml` or
`examples/smart-home/runtime.yaml`:

- Locks (door, gate, or any access-control actuator).
- Ovens, stoves, or any heat-producing appliance beyond HVAC within a comfort band.
- Alarms (arming or disarming a security or life-safety system).
- Industrial machinery start/stop.
- Any safety system (fire suppression, gas shutoff, interlocks, and similar).

## Known limitations

This runtime's safety argument is deliberately narrow. The following gaps are recorded rather than
omitted:

- **The approval binding is context-binding, not authentication.** A keyless sha256 proves which
  exact action an approval covers; it does not prove who granted it. Anyone able to construct an
  `Approval` can compute a valid `binding` (`src/newton_mcp/action/approval.py`).
- **There is no approval revocation or single-use/nonce semantics.** An `Approval` remains valid
  against any matching attempt until `expires_at`; nothing in this repository can invalidate one
  early or mark it "already spent."
- **`redact_args()` misses a secret passed under a benign key name.** Redaction is key-name-only
  and deliberately over-eager (`SECRET_KEY_PATTERNS`, `src/newton_mcp/runtime/audit.py`): a value
  under a harmless key like `note` still reaches the audit log unredacted. Over-redaction is the
  safe direction this module chose; under-redaction of a mislabelled secret is the accepted gap.
- **A stdio transport fingerprint cannot distinguish a credential rotation from an endpoint
  change.** `ServerConfig.transport_fingerprint` hashes the full `env` mapping for a stdio server
  (`src/newton_mcp/runtime/config.py`); rotating a credential in that mapping invalidates
  outstanding approvals exactly as an endpoint change would, which is accepted as fail-safe for
  short-lived approvals but means the two cases are indistinguishable from the fingerprint alone.
- **The gateway ships no authentication.** The container binds `0.0.0.0` by default (see
  `README.md`); exposing it to an untrusted network is the operator's responsibility.
- **The verifier's documented worst-case deadline overrun is one in-flight poll.** No new poll
  *starts* after `contract.verification.timeout_seconds` elapses, but a poll already in flight may
  finish up to `read_timeout_seconds` (default `10.0`) later (`Verifier.verify()`).
- **`announce` has no `read_tool` and is therefore never verifiable.** Any run through it always
  ends `ESCALATED` after exactly one call, confirm-gated but unverified by design (see
  `examples/smart-home/README.md`).

## Non-goals

The following are stated as non-goals of this repository, not as promised future work:

- Signed approvals or an authenticated approver.
- Approval revocation or nonce/single-use semantics.
- An LLM, or any non-deterministic judge, anywhere in the verification path -- `evaluate()` in
  `src/newton_mcp/action/conditions.py` is a pure function.
- LLM- or embedding-based capability matching -- the resolver in
  `src/newton_mcp/runtime/resolver.py` is purely deterministic.
- Long-lived MCP sessions or connection pooling -- a connection is opened per call and closed.
- Exposing the runtime itself as MCP tools -- no tool for approving, executing, or verifying an
  action is registered on `create_server()`.
- Any unsafe actuator class -- see "Demo-safe actions," above.
