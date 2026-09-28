# Action runtime: MCP host, tool discovery, policy and context-bound approval

> This document describes `src/newton_mcp/runtime/` and `src/newton_mcp/action/policy.py` /
> `src/newton_mcp/action/approval.py`, **this project's experimental proposal** for Direction A
> (MCP as Newton's action boundary). It is not an Archetype standard, and nothing described here
> has been run against a live Newton account or a live MCP actuator server. This package
> discovers tools, resolves candidates, decides auto/confirm/deny and can bind an approval to one
> exact action -- it still executes nothing: no `call_tool`, no lifecycle, no audit, no LLM.

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

Later phases (not this one) turn a chosen `CandidateAction` into an actual `call_tool`, subject
to policy, human approval, execution and outcome verification.

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
    read_tool: get_room_temperature   # optional; used for later verification, and scored
    idempotent: true                  # optional, default false; retry-safety metadata only
```

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
  spuriously valid one.
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

## What this package does not do

- Execute tools (`call_tool`), retry, or change the physical world in any way.
- Track an action's lifecycle, audit an approval, or verify a physical outcome.
- Enforce that a caller hands `verify_approval` the `policy_version` of the policy currently
  loaded -- it only makes a mismatch detectable; wiring that coupling in is a later, executor
  issue.
- Revoke an approval, or give it single-use/nonce semantics.
- Match capabilities with an LLM or an embedding model -- v0 is purely deterministic.
- Expose itself as MCP tools, or wire into `create_server()` / `newton_mcp.config.Settings`. No
  new MCP tool is registered for approving an action.
- Hold long-lived MCP sessions, pool connections, or reconnect with backoff.

See `docs/architecture.md` for how this fits into the full proposed pipeline (capability
resolver -> policy -> approval -> executor -> verifier), and the repository's `README.md` for
what is confirmed Archetype behaviour versus this project's proposal.
