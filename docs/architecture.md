# Architecture

## Terminology (Archetype's, not ours)

- **Newton** — the physical-world foundation model. Exposed on `POST /query` as two families:
  Newton C (`Newton::c2_...`, text/image/video reasoning, structured output) and Omega encoders
  (`OmegaEncoder::...`, time-series → 768-dim embeddings per channel).
- **Physical Agent** — an application built on Newton that continuously interprets physical inputs.
- **Newton Agents** — packaged blueprints (`osm`, `anomaly-discovery`, `rare-event-detection`,
  `task-verification`, `manual-generation`) run through the Agents API: blueprint → bundle → run,
  with paginated results/events/logs.

## Direction B — Newton as an MCP capability (implemented, mock-validated)

```
MCP host ──MCP──► newton-mcp-gateway ──HTTPS──► {ATAI_API_ENDPOINT}/query
                        │
                        └── NewtonBackend protocol
                              ├── MockNewtonBackend      (default, no credentials)
                              └── ArchetypeNewtonBackend (NEWTON_BACKEND=api)
```

Design rules:
- Request/response models mirror the documented API field-for-field; the gateway adds
  validation and MCP ergonomics, not a new abstraction.
- Few tools, each mapped to a documented usage pattern. Tools are annotated `read_only`.
- Mock output is always labelled and never looks like real inference.

## Direction A — MCP as Newton's action boundary (proposal)

Newton itself does not need to speak MCP. A small **action runtime** acts as the MCP *host*:

```
Newton / Newton Agent
        │  Physical Action Contract (what should happen)
        ▼
  Action Runtime  ── MCP client ──► MCP servers (how it happens)
   ├─ capability resolver   (discover tools, match target/location/args)
   ├─ policy engine         (auto / confirm / deny)        ← implemented, deterministic
   ├─ approval              (tied to the exact normalized action)  ← implemented, see below
   ├─ executor              (MCP tool calls, idempotent where possible)  ← implemented, mock-validated
   └─ verifier              (observe → did the physical outcome happen? → retry / escalate)  ← implemented, mock-validated
        │
        └──────────────── feedback to Newton (new observation)
```

Lifecycle: `PROPOSED → AUTHORIZED | DENIED`, `AUTHORIZED → EXECUTING`,
`EXECUTING → EXECUTED | UNKNOWN`, `EXECUTED → VERIFYING`, `UNKNOWN → VERIFYING | ESCALATED`,
`VERIFYING → SUCCEEDED | FAILED | ESCALATED`, `FAILED → EXECUTING (retry) | ESCALATED`. `UNKNOWN`
cannot return directly to `EXECUTING` -- an unobserved outcome must be verified or escalated,
never blindly retried. See `docs/action-runtime.md`.

Correlation ids to carry through every step: `observation_id`, `action_id`, `tool_call_id`,
`verification_id`.

### Digital success is not physical success

A successful MCP tool call is not a successful physical action: `set_target_temperature(23)` can
return `200 OK` while the AC is offline, and a timeout means the runtime does not know whether
anything happened at all. The retry rule this proposal implements (`newton_mcp.runtime.executor`,
`newton_mcp.runtime.verifier`) is one sentence: **verify before you ever retry, and never re-send
a non-idempotent action whose outcome is unknown.** Concretely: an `EXECUTING -> EXECUTED` or
`EXECUTING -> UNKNOWN` outcome is always verified through the capability's `read_tool` before any
retry decision is made; a verified failure only retries when the capability is `idempotent` and
the contract's `retry_limit` has not been exhausted; everything else escalates to a human. A failed or unreadable latest poll escalates even after
an earlier negative reading; stale evidence never licenses a retry. Reads and sleeps consume
the remaining verification deadline, and late results are discarded. See
`docs/action-runtime.md` for the full executor/verifier behaviour.

## Safety defaults

See `docs/safety.md` for the safety defaults table, the numeric defaults table, and the safety
invariants -- this section is a pointer, not a copy, so the table exists in exactly one place.
