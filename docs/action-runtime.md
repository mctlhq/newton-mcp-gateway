# Action runtime: MCP host, tool discovery and deterministic capability resolver

> This document describes `src/newton_mcp/runtime/`, **this project's experimental proposal**
> for Direction A (MCP as Newton's action boundary). It is not an Archetype standard, and
> nothing described here has been run against a live Newton account or a live MCP actuator
> server. Everything in this package is **discovery and resolution only** -- it executes
> nothing: no `call_tool`, no policy evaluation, no approval, no lifecycle, no audit, no LLM.

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
ranks or identifies on it. `CandidateAction.server_identity` always stays the configured
identity from `runtime.yaml`. What a server's canonical identity is (config label, transport
fingerprint, observed `serverInfo`, or some combination) and what an approval binds to is a
question for a later, security-relevant proposal -- this package only makes the observed value
available.

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

## What this package does not do

- Execute tools (`call_tool`), retry, or change the physical world in any way.
- Evaluate policy, bind an approval, track an action's lifecycle, or verify an outcome.
- Match capabilities with an LLM or an embedding model -- v0 is purely deterministic.
- Expose itself as MCP tools, or wire into `create_server()` / `newton_mcp.config.Settings`.
- Hold long-lived MCP sessions, pool connections, or reconnect with backoff.

See `docs/architecture.md` for how this fits into the full proposed pipeline (capability
resolver -> policy -> approval -> executor -> verifier), and the repository's `README.md` for
what is confirmed Archetype behaviour versus this project's proposal.
