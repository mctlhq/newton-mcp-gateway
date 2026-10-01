# Action runtime: MCP host, tool discovery, policy, approval, execution, verification, audit

> This document describes `src/newton_mcp/runtime/` and `src/newton_mcp/action/policy.py` /
> `src/newton_mcp/action/approval.py` / `src/newton_mcp/action/conditions.py`, **this project's
> experimental proposal** for Direction A (MCP as Newton's action boundary). It is not an
> Archetype standard, and nothing described here has been run against a live Newton account or a
> live MCP actuator server -- every result in this document and its tests is **mock-validated**
> only, using in-process fakes (no subprocess, no socket, no credentials). This package discovers
> tools, resolves candidates, decides auto/confirm/deny, binds an approval to one exact action,
> calls the chosen MCP tool with a bounded timeout, re-observes the world through a `read_tool`,
> and tracks the whole thing through an explicit lifecycle with an append-only audit trail. It
> still consults no LLM anywhere in the runtime path.

## Why this exists

`newton_mcp/action/contract.py` defines the `PhysicalActionContract`: a tool-independent
description of a desired physical-world outcome (*what* should happen, and why).
`newton_mcp/action/propose.py` turns a Newton observation into a validated contract. Neither
of those knows *how* a contract could be satisfied -- no code in the repo, before this
package, ever looked at an MCP tool and asked "could this tool do that?"

The runtime is the missing link. The gateway process also becomes an MCP **host/client**: it
reads an allow-list (`runtime.yaml`), connects to the configured MCP servers, discovers their
tools with `list_tools`, and resolves a contract into a ranked list of concrete
`CandidateAction`s, each carrying an explicit `why`. Every rejected capability also carries an
explicit reason, because a resolver that cannot explain a rejection cannot be trusted with a
physical actuator.

A chosen `CandidateAction` is then subject to policy, human approval, and finally execution and
outcome verification -- described in the sections below.

## `runtime.yaml`: the allow-list

Set `NEWTON_MCP_RUNTIME_CONFIG` to the path of a `runtime.yaml` file and load it with
`newton_mcp.runtime.load_runtime_config()`. See `examples/runtime.example.yaml` for a full,
safe example (HVAC over stdio, lighting and a speaker announcement over streamable-http).

```yaml
servers:
  - name: hvac-controller
    transport:
      kind: stdio          # or: kind: streamable-http, url: https://...
      command: hvac-mcp-server
      args: ["--config", "/etc/hvac-mcp-server/config.yaml"]
    # identity: hvac-controller   # optional; defaults to `name`

  - name: home-bridge
    transport:
      kind: streamable-http
      url: https://home-bridge.local/mcp
      # auth:                       # optional; only for streamable-http. See
      #   header: Authorization     # "Authenticated streamable-http" below.
      #   scheme: Bearer            # omit to send the raw value instead
      #   env: ALICE_MCP_TOKEN      # the *name* of an env var; never a literal value here

capabilities:
  - server: hvac-controller
    tool: set_target_temperature
    goal_prefixes: ["reduce_room_temperature", "raise_room_temperature"]
    target:
      type: environment
      locations: ["kitchen", "living_room"]   # empty/omitted = wildcard
    arguments:
      location: "${target.location}"
      target_temperature_c: "${constraints.desired_temperature_c}"
    read_tool: get_room_temperature   # optional; used for verification, and scored
    read_arguments:                   # optional; rendered like `arguments`, passed to read_tool
      location: "${target.location}"
    idempotent: true                  # optional, default false; retry-safety metadata only
```

`read_arguments` renders through the same `render_arguments(...)` as `arguments` -- the same
`${target.*}`/`${constraints.*}` roots, the same refusal of `verification.*` as an unknown root --
and is carried on the resolved candidate as `CandidateAction.read_args`. An absent
`read_arguments` renders to `{}`. The verifier calls `read_tool` with exactly `read_args`, never
the action's own `args`, which could otherwise carry actuator parameters (e.g.
`target_temperature_c`) into a read-only tool.

Every model forbids unknown keys. A `runtime.yaml` that fails to validate fails loudly at load
time -- there is no fallback to a default config, and no partial config is ever accepted. A
server's `identity` defaults to its `name`; two servers may never resolve to the same identity,
because `CandidateAction.server_identity` must name exactly one server.

## The catalog: discovery only

`CapabilityCatalog.refresh()` connects to every configured server (following the `ClientFactory`
seam -- the default factory maps `stdio` to a subprocess `Client` and `streamable-http` to a
network `Client`), calls `list_tools` (paginating to exhaustion), and disconnects. It issues no
other MCP request -- in particular it never calls `call_tool`; a test in `tests/runtime/`
asserts this by flipping a flag in a fake tool's body and checking it stays `false` after a full
`refresh()`.

One `anyio.fail_after(server_timeout_seconds)` scope bounds the **whole** per-server cycle:
connect, the initialize handshake, and every `list_tools` page. A server that connects and then
hangs mid-listing times out exactly like one that never connects, and cannot block discovery of
the remaining servers. Only `Exception` (including `ExceptionGroup`) is caught per server; task
or process cancellation (`BaseException`/`BaseExceptionGroup`) always propagates out of
`refresh()` untouched, and the previous snapshot is left in place because a cancelled refresh
never assigns.

Only allow-listed tools survive into the catalog. A tool a server advertises but the allow-list
does not name is dropped silently -- it was never "ours". An allow-listed tool missing from a
server's listing becomes a `tool_missing` problem, and the rest of the catalog stays usable; a
missing `read_tool` becomes `read_tool_missing` but the capability itself still resolves. A
server that cannot be reached becomes one `server_unavailable` problem, and the rest of the
servers are still discovered.

The catalog also records each server's observed MCP `serverInfo` (`name`, `version`) from the
initialize handshake, as **metadata only** -- neither the catalog nor the resolver filters,
ranks or identifies on it, and it is never used for an approval binding (see below): it is
self-asserted by the remote server during the handshake, so a substitute server could echo
whatever `name`/`version` an approval expects, and a benign version bump would otherwise
invalidate every live approval. `CandidateAction.server_identity` always stays the configured
identity from `runtime.yaml`, for display and logs.

### Server identity in an approval binding

An approval (below) needs a stronger identity than the configured label alone. If it bound only
to `server_identity`, re-pointing a server's `url`/`command`/`args` under an unchanged `name`
would leave every outstanding approval valid against a substituted server -- the approval would
become a bearer token for a tool *name*, not for a tool. `ServerConfig.binding_identity` closes
that gap: it is `f"{resolved_identity}@sha256:{transport_fingerprint}"`, where
`transport_fingerprint` is a sha256 of the server's declared transport:

- `streamable-http`: `{"kind": "streamable-http", "url": <canonical url>}`. Canonicalisation
  lowercases only the scheme and the host-name part of the netloc, and elides a port equal to the
  scheme default (443 for `https`, 80 for `http`). Userinfo (`user:pass@`), IPv6 brackets, path,
  query and fragment stay byte-exact -- two URLs that differ only in userinfo fingerprint
  differently. Nothing else is normalised, so percent-encoding and trailing-slash differences
  also fingerprint differently: the failure mode is a spuriously invalidated approval, never a
  spuriously valid one. When the server declares `auth` (see "Authenticated streamable-http"
  below), the digest gains a third key: `"auth": {"header": ..., "scheme": ..., "env": ...}` --
  the header *name*, the scheme, and the environment variable's *name*, never its value, which
  is never read while computing a fingerprint. Rotating only the variable's value therefore
  leaves `binding_identity` (and so every outstanding approval) unchanged; changing the `env`
  name, the `header`, or the `scheme` changes it, exactly like changing `url` does. A server
  with no `auth` block produces the same two-key dict as before this existed.
- `stdio`: `{"kind": "stdio", "command": ..., "args": [...], "env": {...}}`, with `command`,
  `args`, and the **full `env` mapping (names and values)** byte-exact -- no `PATH` lookup, no
  filesystem resolution. A stdio server's target is often configured through `env` (e.g.
  `HA_URL`), so this fingerprint cannot tell a credential rotation from an endpoint change;
  rotating an env value therefore also invalidates outstanding approvals, which is accepted as
  fail-safe for short-lived approvals. Env values enter only the sha256 input: they never appear
  in `binding_identity`, an `Approval`, a reason string, or a log line.

`CandidateAction.server_binding_identity` carries this composite value alongside the unchanged
`server_identity` label. Changing a server's `url`, `command`, `args`, or any `env` name or value
under an unchanged `name`/`identity` therefore invalidates every approval issued before the
change.

### Authenticated streamable-http

**Mock-validated only** -- see the honesty note at the top of this document. A `streamable-http`
server may declare a single static auth header:

```yaml
transport:
  kind: streamable-http
  url: https://home-bridge.local/mcp
  auth:
    header: Authorization    # any valid HTTP field name, but not one the SDK manages itself
                              # (content-type, accept, mcp-session-id, mcp-protocol-version)
    scheme: Bearer            # optional; omit to send the variable's raw value as the whole header
    env: ALICE_MCP_TOKEN      # the *name* of an environment variable -- never a literal value
```

`HttpTransport.auth` is loaded by `newton_mcp.runtime.config.HttpAuth`. The value itself is never
written to `runtime.yaml`, never stored on any model, and never read while loading or
fingerprinting the config -- only `newton_mcp.runtime.auth.resolve_auth_header()` reads it, at
connect time, inside `default_client_factory()`. A raw literal secret under any other key (for
example `auth: {header: Authorization, value: "..."}`) is rejected at load time with
`newton_mcp.runtime.config.AuthConfigError`, which is deliberately **not** a `ValueError`: a
`ValueError`/`AssertionError` raised from inside a Pydantic validator is wrapped into a
`ValidationError` whose rendered message echoes the rejected input, which would print the secret
right back out. `AuthConfigError`'s message names only fixed field paths and, at most, a
validated header/env-var/server name -- never a rejected value. The same screen fires for `auth`
on a `stdio` transport (unsupported; stdio already passes credentials through its own `env` map)
and for an unrecognised transport `kind`, both before Pydantic's own machinery gets a chance to
build an error around the raw block.

**Rotation vs. re-pointing.** Because only the header name, scheme and env-var *name* enter the
fingerprint (see above), rotating the credential -- changing what `ALICE_MCP_TOKEN` is set to --
never invalidates an outstanding approval; the runtime resolves the header value fresh on every
connect. Re-pointing `env:` at a different variable, or changing `header`/`scheme`, changes
`binding_identity` exactly like changing `url` does, so `Executor._resolve_server()` refuses to
call a candidate resolved before that change.

**Failure is loud, not silent.** If the named variable is unset or blank at connect time,
`resolve_auth_header()` raises `MissingAuthSecret` naming the variable and the server -- no
transport is opened and the runtime never connects unauthenticated. During
`CapabilityCatalog.refresh()` this becomes one `server_unavailable` `CatalogProblem` for that
server (naming the same variable), while every other configured server is still discovered.
Any other transport failure for an authenticated server is recorded as a safe, class-only
diagnostic (`"transport failure (<ExceptionClassName>)"`), never the raw exception text --
an httpx2/anyio error can embed request state, and the resolved header travelled on that exact
request.

The factory enforces this boundary for builder, connect, list, call and close failures,
including nested exception groups: public exceptions and SDK logs carry only exception
classes. A logging record factory applies this policy only during authenticated SDK
operations (including their child tasks); caller-body exceptions and ordinary caller logs
retain their behavior. Cancellation members are preserved. SDK and HTTP contexts are closed
on success, failure and cancellation, and teardown cannot replace a caller-body exception.

Header names, schemes and env names must match their complete token grammar, including at
the final character. Nonblank credentials are preserved without trimming, but all C0
controls and DEL, and values that the locked HTTP client's ASCII header encoder cannot
encode, are rejected before creating a client. The SDK does not follow streamable-HTTP
redirects; authenticated headers are never forwarded to a redirect destination.

**Non-goals.** This is a single static header, resolved once per connect -- not an OAuth 2.1
client. There is no token store, no refresh flow, no discovery, no dynamic client registration,
and no support for the real `mctlhq/mctl-alice` server's OAuth authorization-code flow (see
`examples/smart-home/README.md`). `mcp>=2.2` ships an OAuth client under `mcp/client/auth/` for
that; it is out of scope here.

## The resolver: deterministic, synchronous, explainable

`Resolver.resolve(contract)` reads the catalog's last snapshot and evaluates every configured
capability through one fixed, ordered filter chain. The **first** failing stage is the recorded
rejection, so a reason is always the most specific true one:

1. `server_unavailable` / `tool_missing` -- from catalog problems.
2. `goal_prefix` -- no configured prefix is a prefix of `contract.goal`.
3. `target_type` -- `capability.target.type != contract.target.type` (exact, case-sensitive).
4. `target_location` -- if `capability.target.locations` is non-empty, `contract.target.location`
   must match one case-insensitively; an empty `locations` list is a wildcard.
5. `template_error` -- rendering `capability.arguments` against the contract failed.
6. `schema_mismatch` -- the rendered arguments do not validate against the discovered tool's
   `input_schema`. An invalid schema on the server side is itself a `schema_mismatch`
   rejection, never an exception out of `resolve()`.

A capability that survives every stage becomes a `CandidateAction`. When more than one survives,
they are ranked by descending `score`, tie-broken deterministically by
`(server_identity, tool_name)`. Scoring is a small, documented sum:

```
score = BASE_SCORE (0.50)
      + GOAL_WEIGHT (0.20) * len(longest matching goal prefix) / len(contract.goal)
      + LOCATION_BONUS (0.20)    if the capability matched an explicit location
      + READ_TOOL_BONUS (0.10)   if a read_tool is configured AND was discovered
```

`idempotent` never affects the score -- it is retry-safety metadata a later phase consumes, not
a measure of match quality. `resolve()` is fully synchronous: it makes no network call of its
own and consults no LLM, so "no I/O" is structural, not a promise.

### Argument templates

`capability.arguments` values may contain `${path}` placeholders resolved against the contract.
A value that is exactly `"${path}"` is replaced by the resolved value, **preserving its JSON
type** (so `"${constraints.desired_temperature_c}"` becomes the int `23`, satisfying an
`integer` schema). A placeholder embedded in a longer string is interpolated via its string
form. Dicts and lists render recursively. There is no escape sequence in v0.

Supported roots: `goal`, `reason`, `confidence`, `target.type`, `target.location`,
`target.resource`, and `constraints.<key>`. `verification.*` is deliberately **not** a
template root: tool arguments must never be derived from the verification section. An unknown
root, or a `constraints.<key>` the contract does not carry, is rejected as a `template_error`
naming the exact placeholder -- never guessed, defaulted, or silently dropped.

## Policy: `policy.yaml`

Set `NEWTON_MCP_POLICY_PATH` to the path of a policy file and load it with
`newton_mcp.action.load_policy()`. See `examples/policy.example.yaml` for a full, safe example.
Like `runtime.yaml`, every model forbids unknown keys and there is no fallback to a default: a
policy file that fails to load or fails its schema raises loudly, and `Policy.conservative()` is
never an implicit fallback -- it is only ever returned when a caller asks for it by name.

```yaml
policy_version: "example.v1"   # required, non-empty; pinned by an Approval
default: deny                  # decision when no rule matches
rules:
  - name: hvac-within-comfort-band
    goal_prefix: reduce_room_temperature
    tool_name: set_target_temperature
    max_risk: low
    min_confidence: 0.8
    arg_ranges:
      target_temperature_c: { min: 20, max: 25 }   # inclusive
    decision: auto
```

`Policy.evaluate(contract, candidate=None)` decides in this order:

1. `contract.risk is critical` always denies, before any rule is consulted.
2. Rules are walked in file order; the first one whose predicates all match wins. Predicates,
   checked in order: `goal_prefix` (a prefix of `contract.goal`), `tool_name` (exact,
   case-sensitive match against `candidate.tool_name`), `max_risk` (a ceiling on
   `contract.risk`), `min_confidence` (a floor on `contract.confidence` -- a `None` confidence
   never satisfies a non-zero floor), then `arg_ranges` last, evaluated only once every earlier
   predicate matched. A rule declaring `tool_name` or `arg_ranges` never matches when no
   `candidate` was supplied, so evaluation falls through toward `default` rather than matching on
   fewer predicates than it declares. An `arg_ranges` check that finds a missing argument, a
   non-numeric value (a `bool` does not count, even though it subclasses `int`), or a value
   outside its inclusive bound returns `deny` **immediately**, naming the argument -- it never
   falls through to a later, broader rule that would have auto-approved the very value this rule
   forbade.
3. No rule matched: `self.default` (itself defaults to `deny`).
4. A confirmation ceiling applies to whatever decision was reached in 2 or 3: if
   `contract.requires_confirmation` is true, an `auto` decision is raised to `confirm`; `confirm`
   and `deny` pass through unchanged. This is a ceiling, not an upgrade -- a model-authored
   contract flag must never be able to grant more authority than the operator's rules do, so a
   `deny` outcome stays `deny` even when the contract also asks for confirmation.

## Contract v0.2 at a glance

`src/newton_mcp/action/contract.py` defines `PhysicalActionContract`. `version` is pinned to
`^0\.2$` (default `"0.2"`) with **no v0.1 migration shim**: this was a breaking, owner-approved
schema change (v0.2 replaced the free-text `Verification.condition` string with the structured
`Condition` described below), there is no database and no persisted contract, and every v0.1
artefact in this repo was updated in the same commit as the model. A v0.1 contract -- a string
`condition`, or `version: "0.1"` -- is rejected loudly by Pydantic, never coerced.

**`PhysicalActionContract`**

| Field | Type | Default | Notes |
|---|---|---|---|
| `version` | `str` | `"0.2"` | Pattern `^0\.2$`; any other value fails validation. |
| `goal` | `str` | required | Desired outcome, independent of any specific tool. |
| `reason` | `str` | required | Why the action is proposed. |
| `confidence` | `float \| None` | `None` | `0.0 <= confidence <= 1.0` when present. |
| `target` | `Target` | required | See below. |
| `constraints` | `dict[str, Any]` | `{}` | Free-form; read via `constraints.<key>` template roots. |
| `risk` | `Risk` | required | See the `Risk` ladder below. |
| `reversible` | `bool` | `True` | |
| `requires_confirmation` | `bool \| None` | `None` | Explicit override; `None` leaves the decision to policy. It is a confirmation *ceiling*, never a downgrade -- see Policy, above. |
| `verification` | `Verification` | required | See below. |
| `evidence` | `Evidence \| None` | `None` | See below. |

**`Risk`** (`newton_mcp.action.contract.Risk`, a `StrEnum`), ascending: `read_only`, `low`,
`medium`, `high`, `critical`. `critical` always denies before any policy rule is consulted (see
Policy, above); this ladder is also the order `PolicyRule.max_risk` compares against.

**`Target`** (`extra="allow"` -- a capability's own fields may ride along):

| Field | Type | Default | Notes |
|---|---|---|---|
| `type` | `str` | required | e.g. `"environment"`, `"machine"`, `"zone"`. |
| `location` | `str \| None` | `None` | Human-readable, e.g. `"kitchen"`. |
| `resource` | `str \| None` | `None` | Stable asset identifier if known. |

**`Verification`**

| Field | Type | Default | Notes |
|---|---|---|---|
| `condition` | `Condition` | required | A predicate `{path, op, value}`, or an `{all: [...]}`/`{any: [...]}` composite -- see below. |
| `timeout_seconds` | `int` | `300` | `>= 1`. Bounds the verifier's polling deadline. |
| `retry_limit` | `int` | `0` | `>= 0`. With the default, the first verified failure escalates; see the retry rule, below. |

**`Evidence`** (provenance: which observation led to this contract):

| Field | Type | Default |
|---|---|---|
| `observation_id` | `str \| None` | `None` |
| `summary` | `str \| None` | `None` |

The committed JSON Schema, generated from this model, lives at
`schemas/physical-action-contract.schema.json`; `tests/test_action_contract.py` asserts it is
byte-for-byte equal to `PhysicalActionContract.model_json_schema()`. Regenerate it whenever the
model changes -- the command lives in `CONTRIBUTING.md`'s "Regenerating the JSON schema" section.

## Verification conditions (contract v0.2)

`src/newton_mcp/action/conditions.py` replaces the v0.1 free-text `Verification.condition`
string with a structured `Condition`: exactly one of a predicate object `{path, op, value}` (`op`
one of `eq | ne | lt | le | gt | ge`), an `{all: [Condition, ...]}` conjunction, or an
`{any: [Condition, ...]}` disjunction, resolved through a callable Pydantic `Discriminator` (the
wire shape has no `kind`/tag field). There is no expression language and no parser anywhere in
this module or the rest of the repo -- a reviewer audits a condition by reading the JSON, and a
Newton model under the strict-JSON prompt cannot emit an expression the runtime silently
misreads. Nesting is capped at `MAX_CONDITION_DEPTH = 8`, enforced at validation time.

`evaluate(condition, observation) -> ConditionResult(satisfied, reason, known)` is a pure function: no
I/O, no LLM, and it never raises for a malformed comparison. `path` is a dotted path resolved by
mapping traversal only (no list-index traversal in v0.2); a segment that is missing, or that
encounters a non-mapping or `None` before the path is exhausted, is *not found* -- and a
not-found path is never satisfied, for every `op` including `ne`. `eq`/`ne` compare only
type-compatible operands (`bool` is never a number, the same rule `action/policy.py` already
applies to `arg_ranges`); `lt|le|gt|ge` require both operands to be non-bool `int`/`float`. Any
other pairing is a type mismatch: not satisfied, never an exception. Every reason names the
`path`, the `op` and the contract's expected `value`, and the observed value's *type* -- never
the raw observed value, following `approval.py`'s "a failure reason never echoes an args value".
`known=False` distinguishes missing paths, incompatible types and non-finite numbers from a
known negative comparison. Such data never establishes a verified failure. `all` is satisfied
only if every child is; an unknown child makes a negative result unknown. `any` is satisfied
if at least one known child is satisfied; otherwise an unknown child makes its result unknown.

This is a breaking, owner-approved schema change: `PhysicalActionContract.version` moves to
`^0\.2$` with no v0.1 migration shim, since there is no database, no persisted contract, and every
v0.1 artefact that existed in this repo was updated in the same commit as the model. A v0.1
contract (a string `condition`, or `version: "0.1"`) is rejected loudly by Pydantic.

## Approval: binding to one exact action

`src/newton_mcp/action/approval.py` defines `Approval`, `compute_binding`, `create_approval` and
`verify_approval`. An `Approval.binding` is a sha256 over the canonical JSON of exactly
`{server_identity, tool_name, args, action_id, policy_version, expires_at}` -- `server_identity`
here is `candidate.server_binding_identity`, the full `args` mapping (not its digest) enters, and
`expires_at` is rendered via the canonical timestamp form (`src/newton_mcp/canonical.py`, aware
datetimes only, UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`). `approved_by`, `approved_at` and
`approval_id` are deliberately **not** bound -- audit and an authenticated approver are issue #7's
concern, not this one's.

**`binding` is context-binding, not authentication.** A keyless sha256 proves which exact action
an approval covers -- it does not prove who granted it. Anyone able to construct an `Approval`
can compute a valid `binding`; signed approvals and an authenticated approver stay out of scope.

`verify_approval(approval, candidate, action_id, policy_version, now)` returns valid only if
`now` is before `approval.expires_at` and the binding recomputed from `candidate`, `action_id`,
`policy_version` and `approval.expires_at` equals the stored `binding`, checked with
`hmac.compare_digest`. Any single differing field in the binding payload -- `server_identity`
(so a re-pointed server invalidates it), `tool_name`, any one entry of `args`, `action_id`, or
`policy_version` -- makes verification fail; so does a mutated `expires_at`, even to a
still-future timestamp, because `expires_at` is itself part of the payload the binding covers. A
naive `now` raises rather than silently comparing. A failure's reason names the failing field but
never echoes an `args` value.

## Lifecycle, correlation ids and audit

`src/newton_mcp/runtime/lifecycle.py` defines `ActionState`, an explicit ten-member state
machine, and `transition()`, the single guarded mutation point. The allowed edges live in one
module-level, read-only mapping, `ALLOWED_TRANSITIONS` -- the safety argument is that table, not
control flow:

```
PROPOSED   -> AUTHORIZED | DENIED
AUTHORIZED -> EXECUTING
EXECUTING  -> EXECUTED | UNKNOWN
EXECUTED   -> VERIFYING
UNKNOWN    -> VERIFYING | ESCALATED
VERIFYING  -> SUCCEEDED | FAILED | ESCALATED
FAILED     -> EXECUTING (retry, requires verified_failure=True) | ESCALATED
DENIED, SUCCEEDED, ESCALATED -> (terminal, no outgoing edge)
```

Two edges are absent on purpose, and each absence is the point of the issue this module answers:

- **`UNKNOWN -> EXECUTING` does not exist.** `UNKNOWN` means the runtime does not know whether a
  physical action happened (a tool-call timeout or transport failure, never a synchronous error
  response -- see below). Re-issuing a non-idempotent physical action while its outcome is
  unknown is exactly the failure mode this module exists to prevent, so an unknown outcome must
  be verified or escalated, never blindly retried.
- **`PROPOSED -> EXECUTING` does not exist.** Execution always requires passing through
  `AUTHORIZED` first.

A synchronous MCP error response is still a *completed call attempt* -- it does not prove the
physical action did not (partly) happen -- so it goes `EXECUTING -> EXECUTED -> VERIFYING` like
any other completed call; there is no `EXECUTING -> FAILED` edge. `FAILED` is reachable only from
`VERIFYING`, so a `FAILED` record always means a *verified* failure, and a `FAILED -> EXECUTING`
retry additionally requires an explicit `verified_failure=True` keyword (never inferred from the
free-text `reason` string).

`ActionRecord` is frozen and carries the four correlation ids named above:
`observation_id`, `action_id`, `tool_call_id`, `verification_id`. All four are populated at
`new_action_record()` time -- generated (`obs-`/`act-`/`call-`/`ver-` plus 16 hex characters,
via an injectable `id_factory`) for any the caller omits, so every audit line ever written for a
record carries four non-null ids. `observation_id`/`action_id` are fixed for the whole action;
`tool_call_id`/`verification_id` are **attempt-scoped**: the creation-time pair is attempt 1's,
unchanged through `AUTHORIZED -> EXECUTING` and any entry into `VERIFYING`, and only a retry
`FAILED -> EXECUTING` replaces both together (attempt increments to 2), so a call and its
verification never end up belonging to different attempts.

`transition()` never mutates the record it is given -- it returns a new one, and a rejected
transition (`IllegalTransition`) leaves the input untouched and writes no audit line.

Audit: `src/newton_mcp/runtime/audit.py` defines `AuditEvent` (one accepted transition) and two
sinks. `MemoryAuditSink` is the default -- effectively disabled, and what the test suite uses, so
no test writes a file. `JsonlAuditSink` appends one compact JSON line per event to a file, opened
in append mode per write, so re-opening an existing audit file never truncates it and lines
survive a process restart; each line carries all four ids, `from`/`to`, `reason`, `attempt`,
`verified_failure`, a canonical UTC `at` timestamp, and, when the caller supplied tool arguments,
a redacted `args` mapping plus `args_digest` (`sha256_hex` over the **unredacted** args -- the
same value `Approval.args_digest` already stores for the same action, so an audit line is
provable against the approval it followed).

`load_audit_sink()` reads `NEWTON_MCP_AUDIT_PATH`. Unlike `load_runtime_config()` /
`load_policy()`, an unset or blank value returns the in-memory sink rather than failing loudly --
an audit sink is not an authority boundary the way an allow-list is, and an operator who never set
the variable never asked for a file. A value that *is* set but unusable (parent directory
missing, path is a directory, not writable) still raises `ValueError` naming the variable and the
path, so a typo is loud.

Redaction (`redact_args()`) replaces the value of any argument key whose casefolded name contains
one of the eleven documented substrings in `SECRET_KEY_PATTERNS` (`key`, `token`, `secret`,
`password`, `passwd`, `credential`, `auth`, `bearer`, `cookie`, `session`, `signature`) with a
fixed `[redacted]` marker, recursing into nested mappings and lists, and truncates long surviving
string values (`_MAX_VALUE_CHARS = 500`). This is **key-name only and deliberately
over-eager**: a benign key like `keypad_zone` is redacted too, and there is no value-shape
detection at all -- a secret passed under a harmless key name (e.g. `note`) still reaches the
log. Over-redaction is the safe direction; this gap is documented rather than papered over.

This module executes nothing itself: no `call_tool`, no MCP session, no timeout policy. It only
carries the *possibility* of a retry (the `FAILED -> EXECUTING` edge and its
`verified_failure=True` guard) -- the retry *decision*, and every call, live in
`newton_mcp.runtime.executor` and `newton_mcp.runtime.verifier`, described next.

## Executor: one bounded MCP tool call

`src/newton_mcp/runtime/executor.py` defines `Executor`. `Executor.execute()` is the **only**
code in this package that performs a `-> EXECUTING` transition: `AUTHORIZED -> EXECUTING` for a
first attempt, `FAILED -> EXECUTING` (with `verified_failure=True`) for a retry. Per attempt, in
order:

1. Resolve the candidate's server by `resolved_identity` in the currently loaded `runtime.yaml`.
   Absent, or `binding_identity != candidate.server_binding_identity` (the server was re-pointed
   under the same name) -> `ExecutorError`, raised before any transition, any audit line, or any
   transport is opened.
2. **Re-check the context-bound approval on every attempt, the first one and any retry alike:**
   `verify_approval(approval, candidate, record.action_id, policy_version, now)`. Invalid ->
   `ApprovalRejected` (carrying the failing field's reason), again before any transition, audit
   line or transport. This is what stops a retry whose approval expired between attempts. For an
   `auto` policy decision the caller still issues an `Approval` (`approved_by="policy:<policy
   version>:<rule>"`), so there is exactly one execution path and no approval-less bypass. The
   binding is context binding, not authentication (see Approval, above) -- who may *approve*
   stays the caller's concern.
3. The `-> EXECUTING` transition, carrying the redacted `args` (and `args_digest`) on the audit
   line.
4. One `anyio.fail_after(call_timeout_seconds)` scope covering connect, the MCP handshake and the
   call -- exactly like `CapabilityCatalog.refresh()` bounds discovery.
5. **Any** returned result, including an MCP *error* result, transitions `EXECUTING -> EXECUTED`:
   a completed call attempt does not prove the physical action did not happen, so there is
   deliberately no `EXECUTING -> FAILED` edge to take. A timeout or any other transport
   `Exception`/`ExceptionGroup` transitions `EXECUTING -> UNKNOWN` instead. The persisted reason
   (and `ExecutionOutcome.detail`) records only a classification -- `timeout` or `transport
   failure (<exception class>)` -- never the exception's message: a transport error can embed the
   server URL, and `runtime.yaml` keeps URL userinfo verbatim, so a raw message would carry a
   credential into the append-only audit log past `redact_args()`.
   `BaseException`/`BaseExceptionGroup` (task/process cancellation) propagate untouched, exactly
   as `refresh()` already does.

`newton_mcp.runtime.catalog.default_client_factory` is reused here: `mcp.Client` already speaks both `list_tools` and
`call_tool`, so there is exactly one place in the repo that maps a transport to a client.

The executor captures a private deep copy of the candidate and server transport before
checking the approval or awaiting connection. The same argument snapshot is verified, audited
and sent; mutations to a caller's nested dictionaries during connection cannot alter the
approved command. `run_action()` also snapshots the contract and candidate for the entire run,
and the verifier snapshots its read context.

## Verifier: re-observing the world

`src/newton_mcp/runtime/verifier.py` defines `Verifier`. `Verifier.verify()` transitions
`EXECUTED|UNKNOWN -> VERIFYING`, then, before issuing any poll, checks whether the outcome can be
observed at all:

- The capability declares no `read_tool` (e.g. `announce`) -- always ends `ESCALATED`, since it
  can never be verified.
- `read_tool` was configured but not discovered (`CatalogEntry.read_tool is None`) -- `ESCALATED`.
- The server's current `binding_identity` differs from `candidate.server_binding_identity` (it
  was re-pointed and the catalog refreshed after the candidate was resolved) -- `ESCALATED`, with
  no read call. The executor refuses a re-pointed server before calling; the verifier must refuse
  it too, or it could confirm the action against a different server's physical state.
- The discovered read tool declares `read_only_hint is False` -- `ESCALATED`; the verifier refuses
  to call a tool the server itself says is not read-only. An **unannotated** (`None`) hint is
  allowed, and that fact is recorded on the eventual transition's reason.

If verification can proceed, the verifier polls **only** the `read_tool` (never the action tool)
with exactly `candidate.read_args` (see `read_arguments`, above) -- the first poll issued
immediately at t=0, then every `poll_interval_seconds`, bounded by the contract's
`verification.timeout_seconds` on an injectable `clock`/`sleep` pair (tests drive the deadline
deterministically; production defaults to `anyio.current_time`/`anyio.sleep`). No poll starts
at or after the deadline. Connect, handshake and read are bounded by the smaller of
`read_timeout_seconds` and the remaining verification budget; sleep is clamped to that budget
too. A result arriving at or after the deadline is discarded and escalates the action.

Each result is turned into an observation mapping by `observation_from_result()`: an MCP *error*
result is never an observation, even if it carries structured content -- it counts as a failed
poll (no observation), same as a transport failure. Otherwise: `structured_content` when it is a
mapping, else a single text content block parsed as JSON into an object, else no observation (a
non-JSON or non-object text result never counts).

Every obtained observation is evaluated with `newton_mcp.action.conditions.evaluate()` against
`verification.condition`:

- Satisfied -> `VERIFYING -> SUCCEEDED`, polling stops.
- The deadline is reached and the **latest poll** gave a known negative condition result ->
  `VERIFYING -> FAILED`, a verified failure.
- The latest poll failed, lacked required data, gave incompatible/non-finite values, or arrived
  too late -> `VERIFYING -> ESCALATED`. Earlier negative evidence never licenses a retry after
  observation is lost. A later usable observation can restore the ability to decide success or
  failure. Zero observations also escalates.

## Adding an MCP actuator by configuration only

A new actuator capability is added entirely through `runtime.yaml` and `policy.yaml` -- **no file
under `src/newton_mcp/` is edited**. Worked example: a lighting capability on a new
`streamable-http` server, safely within the demo-safe classes this repository allows (see
`docs/safety.md`).

`runtime.yaml`:

```yaml
servers:
  - name: home-bridge
    transport:
      kind: streamable-http
      url: https://home-bridge.local/mcp

capabilities:
  - server: home-bridge
    tool: set_light_state
    goal_prefixes: ["turn_on_light", "turn_off_light"]
    target:
      type: environment
      locations: ["kitchen", "hallway"]
    arguments:
      location: "${target.location}"
      "on": "${constraints.desired_on}"
    read_tool: get_light_state
    read_arguments:
      location: "${target.location}"
    idempotent: true
```

`policy.yaml` (append a rule; first match wins, so order it before any broader rule that would
otherwise shadow it):

```yaml
rules:
  - name: lighting
    tool_name: set_light_state
    max_risk: low
    min_confidence: 0.8
    decision: auto
```

Point the process at both files, plus (optionally) an audit trail, then refresh the catalog:

```bash
export NEWTON_MCP_RUNTIME_CONFIG=/path/to/runtime.yaml
export NEWTON_MCP_POLICY_PATH=/path/to/policy.yaml
export NEWTON_MCP_AUDIT_PATH=/path/to/action-audit.jsonl   # optional; unset means in-memory only
```

If `home-bridge` sits behind auth, add an `auth` block to its transport (see "Authenticated
streamable-http" above) and export the one extra variable it names, e.g.:

```bash
export ALICE_MCP_TOKEN=...   # only if runtime.yaml declares transport.auth.env: ALICE_MCP_TOKEN
```

`CapabilityCatalog.refresh()` (or a process restart) picks up the new server and capability on
its next discovery pass.

**What the actuator itself must provide**, each tied to the gate that rejects its absence:

- The tool named in `tool` must appear in the server's `list_tools` response, or the capability is
  rejected as `tool_missing` (`runtime/catalog.py`) and never reaches the resolver.
- The rendered `arguments` must validate against the tool's own `input_schema`, or the resolver
  rejects the candidate as `schema_mismatch` (`Resolver._schema_mismatch()` in
  `runtime/resolver.py`).
- A `read_tool` must exist and, when called with `read_arguments`, return a JSON object --
  `structured_content`, or a single text content block that parses as a JSON object
  (`observation_from_result()` in `runtime/verifier.py`) -- or the verifier can obtain no
  observation and the run always ends `ESCALATED`, never `SUCCEEDED` or a verified `FAILED`.
- The read tool must not declare `read_only_hint=False`; the verifier refuses to call a tool the
  server itself marks as not read-only, and escalates without calling
  (`Verifier._unverifiable_reason()`). An unannotated (`None`) hint is allowed.
- `idempotent: true` is required for `run_action()`'s retry rule to ever retry a verified failure
  at all -- with `idempotent: false` (the default), any verified failure escalates immediately
  regardless of `retry_limit`.

`examples/smart-home/README.md`'s "Why a real Alice server is not drivable yet" section is the
worked negative example: a real actuator missing structured read output, sitting behind
unsupported auth, or addressing targets in a shape this runtime's resolver does not match, cannot
be wired in by configuration alone until those gaps are closed.

The safety defaults an operator should check before enabling any new capability -- action-class
decisions, numeric timeouts, the demo-safe/never-permitted split -- live in `docs/safety.md`, not
here.

## The retry rule: `run_action()`

`run_action()`, in `newton_mcp/runtime/executor.py` next to the attempt counter it depends on,
is the whole issue in one small loop: **verify before you ever retry, and never re-send a
non-idempotent action whose outcome is unknown.**

- An attempt that ends `UNKNOWN` is never retried blindly -- the loop always calls the verifier
  next, and `ALLOWED_TRANSITIONS` has no `UNKNOWN -> EXECUTING` edge to take even if it tried. If
  the outcome the verifier observes is already satisfied, the run ends `SUCCEEDED` having issued
  exactly one tool call.
- A verified `FAILED` retries only when `candidate.idempotent` and `record.attempt <=
  contract.verification.retry_limit` -- otherwise `FAILED -> ESCALATED`, with no further tool
  call. With the default `retry_limit=0` the first verified failure escalates; `retry_limit=1`
  gives exactly one retry (two tool calls total) before escalating.
- `run_action()` delegates every `-> EXECUTING` transition to `Executor.execute()` and never
  performs one itself -- it only decides retry vs. escalate. An `ApprovalRejected` on the first
  attempt propagates with the record left `AUTHORIZED` (nothing was called); on a retry it is
  caught and transitions `FAILED -> ESCALATED`, naming the failing approval field, with no further
  tool call.
- A run always ends in exactly one of `SUCCEEDED` or `ESCALATED`.
- Every transition of the run -- the executor's, the verifier's and `run_action()`'s own
  escalations -- is written to **one** audit sink, `executor.sink`. `run_action()` raises
  `ValueError` before calling anything unless `executor.sink is verifier.sink`, so a run cannot
  split its lifecycle across sinks or drop its terminal transition.

## What this package does not do

- Retry a non-idempotent action, or retry any action whose outcome is unknown and unverified.
- Verify with an LLM or any non-deterministic judge -- `evaluate()` is a pure function.
- Enforce that a caller hands `verify_approval` the `policy_version` of the policy currently
  loaded -- it only makes a mismatch detectable, and the executor's own re-check on every attempt
  is the enforcement point.
- Revoke an approval, or give it single-use/nonce semantics.
- Match capabilities with an LLM or an embedding model -- v0 is purely deterministic.
- Expose itself as MCP tools, or wire into `create_server()` / `newton_mcp.config.Settings`. No
  new MCP tool is registered for approving, executing or verifying an action.
- Hold long-lived MCP sessions, pool connections, or reconnect with backoff -- a connection is
  opened per call (discovery, execution, or a verification poll) and closed.

See `docs/architecture.md` for how this fits into the full proposed pipeline (capability
resolver -> policy -> approval -> executor -> verifier), and the repository's `README.md` for
what is confirmed Archetype behaviour versus this project's proposal. Every result described in
this document is **mock-validated only**: no code here has run against a live actuator or live
Newton credentials.

`examples/smart-home/` composes every module described above into one runnable, mock-validated
end-to-end demo (`propose_action` -> resolve -> policy -> approve -> execute -> verify) against an
in-process fake smart-home actuator, with a committed `trace.jsonl` and an AC-offline failure
path -- see `examples/smart-home/README.md`.
