# Newton MCP Gateway

An experimental [Model Context Protocol](https://modelcontextprotocol.io) bridge for
[Archetype AI Newton](https://www.archetypeai.io), the foundation model for physical-world sensor data.

This project explores two complementary integration patterns:

1. **Newton as an MCP capability** — make physical-world intelligence available to any
   MCP-compatible agent (Claude, ChatGPT, enterprise copilots, custom hosts) through a small,
   auditable gateway over Newton's publicly documented `/query` API.
2. **MCP as an action boundary for Newton** — connect physical-world understanding to external
   digital and physical capabilities through policy-controlled, auditable and *verified* actions.

> **Status: experimental, independent project.**
> The MCP server and mock backend are functional. The real Newton adapter is implemented against
> public documentation but has **not yet been validated against a live account**; it activates only
> with an authorized `ATAI_API_KEY`. The action runtime and the smart-home testbed are
> **mock-validated only** -- no live actuator has been driven by this runtime. This project is not
> affiliated with or endorsed by Archetype AI.

## Why

Newton turns raw sensor streams into physical understanding. MCP is becoming the common
interoperability boundary for agent capabilities. Connecting the two creates value in both directions:

```
        DIGITAL AGENTS                                 PHYSICAL WORLD
  Claude / ChatGPT / enterprise agents          sensors, machines, environments
                 │                                           ▲
                 │ MCP                                       │ actuators (via MCP servers)
                 ▼                                           │
        newton-mcp-gateway  ──────►  Newton  ──────►  MCP Action Runtime
        (Direction B: implemented)   physical         (Direction A: proposal)
                                     understanding    policy · approval · verify
```

The deeper idea is not MCP itself. It is that **Physical AI needs an open, safe, auditable and
closed-loop boundary between understanding the physical world and changing it** — and MCP may be a
good foundation for that boundary.

## Quickstart (no credentials needed)

```bash
git clone https://github.com/mctlhq/newton-mcp-gateway
cd newton-mcp-gateway
uv sync
NEWTON_BACKEND=mock uv run newton-mcp        # stdio MCP server
```

Claude Desktop / Claude Code configuration:

```json
{
  "mcpServers": {
    "newton": {
      "command": "uv",
      "args": ["--directory", "/path/to/newton-mcp-gateway", "run", "newton-mcp"],
      "env": { "NEWTON_BACKEND": "mock" }
    }
  }
}
```

With real Newton access (variable names follow Archetype's docs):

```bash
NEWTON_BACKEND=api ATAI_API_KEY=... ATAI_API_ENDPOINT=https://api.u1.archetypeai.app/v0.5 uv run newton-mcp
```

## Running over HTTP (streamable-http)

The gateway defaults to the `stdio` transport for local MCP hosts. To reach it over HTTP
instead — for a host that is not a local Claude Desktop process on the same machine — set
`NEWTON_MCP_TRANSPORT=streamable-http`:

```bash
NEWTON_BACKEND=mock NEWTON_MCP_TRANSPORT=streamable-http NEWTON_MCP_PORT=8000 uv run newton-mcp
# -> http://127.0.0.1:8000/mcp
```

Or run the container, which enables streamable-http by default:

```bash
docker build -t newton-mcp-gateway .
docker run --rm -p 8000:8000 newton-mcp-gateway
# -> http://127.0.0.1:8000/mcp
```

The container binds `0.0.0.0`, which is intended for a container network. The gateway
ships no authentication, so exposing it to an untrusted network is the operator's
responsibility — put a proxy or firewall in front of it.

## Tools

Deliberately few. Each maps to a documented Direct Query pattern.

| Tool | Newton model family | What it does |
|---|---|---|
| `newton_query` | Newton C (text / image / video reasoning) | Natural-language question grounded in inline text/JSON events or uploaded `file_ids`. Use `system_prompt` to force structured JSON. |
| `newton_embed_timeseries` | Omega encoder | Channel-first sensor window → one 768-dim embedding per channel. |
| `newton_analyze_image` | Newton C (image reasoning) | Ask a question about one image: inline `image_base64`/`mime_type` (sent as a `data.base64_img` event) or an existing `file_id`. Stateless — never uploads or stores anything. See `docs/newton-api-notes.md` for the documented fields this tool sends. |
| `newton_propose_action` | Newton C (text reasoning) | Read-only: observation → exactly one validated Physical Action Contract, or a failure with the raw model text. Proposes only; executes nothing. |

Planned (see issues): running Newton Agent bundles (`osm`, `anomaly-discovery`,
`rare-event-detection`, `task-verification`) and paging their results.

Mock results are labelled `backend: "mock"` and text outputs start with `[mock]`. They are never
presented as real Newton output.

## Direction A: the Physical Action Contract (proposal)

A successful MCP tool call is not a successful physical action. `set_temperature(23)` can return
`200 OK` while the AC is offline, the wrong zone was changed, or the room simply does not cool.
Physical actions need **policy, approval and outcome verification** on top of ordinary tool calling.

This project proposes a tool-independent **Physical Action Contract** (`schemas/`, v0.2):

```json
{
  "goal": "reduce_room_temperature",
  "reason": "The occupied kitchen reached 29.4 C",
  "confidence": 0.96,
  "target": { "type": "environment", "location": "kitchen" },
  "constraints": { "desired_temperature_c": 23, "minimum_temperature_c": 20, "maximum_temperature_c": 25 },
  "risk": "low",
  "verification": {
    "condition": { "path": "temperature_c", "op": "le", "value": 24 },
    "timeout_seconds": 600
  }
}
```

`verification.condition` is a structured object, never an expression string: a predicate
`{path, op, value}` (`op` one of `eq | ne | lt | le | gt | ge`), or an `{all: [...]}` / `{any:
[...]}` composite of predicates. There is no expression parser and no `eval` anywhere in the
repo -- a reviewer audits a condition by reading the JSON.

Newton says *what* should happen. The MCP environment knows *how*. A small action runtime sits in
between:

```
contract → capability match (MCP tool discovery) → policy (auto / confirm / deny)
        → human approval if required → execute → observe → verify → succeed / retry / escalate
```

The contract can be produced by a Newton `/query` with a strict JSON system prompt — a documented
usage pattern — or by a Newton Agent's output. The full pipeline above — capability resolver,
policy engine, approval, executor and verifier — is implemented and mock-validated; see
`docs/action-runtime.md`.

The `newton_propose_action` MCP tool implements the first link of that chain: it sends the caller's
observation (`text_events` / `json_events`) to Newton C with a strict JSON system prompt that embeds
the contract's schema verbatim and, when `allowed_goals` is given, restricts which goals the model
may propose. The model's response is parsed with no repair — no fence stripping, no substring
extraction. If it does not validate against `PhysicalActionContract`, the tool retries exactly once
with the validation errors appended to the prompt, then gives up: it returns `status: "failed"` with
the raw model text and the accumulated errors, never a guessed or partially filled contract. A
backend-reported failure is terminal with no retry. This is mock-validated only — the mock backend
returns the repo's example contract, clearly labelled `backend: "mock"` and with `reason` prefixed
`[mock] `, so it can never be mistaken for real inference.

The first real actuator testbed is a smart-home MCP server (lights, HVAC, speaker), chosen because it
is real hardware with benign, reversible actions. The interface itself is domain-independent:
Home Assistant, BMS, OPC-UA, ROS or enterprise workflow MCP servers plug in the same way.

`src/newton_mcp/runtime/` implements the first box of that pipeline: the gateway process also
becomes an MCP **host/client**. Given a reviewable `runtime.yaml` allow-list, it discovers tools
on the configured MCP servers (`list_tools` only, never `call_tool`) and deterministically
resolves a `PhysicalActionContract` into ranked, explainable candidate tool calls.
`src/newton_mcp/action/policy.py` then decides auto/confirm/deny against a reviewable
`policy.yaml`, and `src/newton_mcp/action/approval.py` can bind an `Approval` to one exact
resolved action (server, tool, args, action id, policy version and expiry) via a sha256 binding
that a re-pointed server or a changed argument invalidates. `src/newton_mcp/runtime/lifecycle.py`
tracks an approved action through an explicit `ActionState` machine (including an `UNKNOWN` state
for a tool-call timeout, which can never go straight back to `EXECUTING`) with four correlation
ids, and `src/newton_mcp/runtime/audit.py` writes one append-only, redacted JSONL line per
accepted transition. `src/newton_mcp/runtime/executor.py` then calls the chosen MCP tool with a
bounded timeout, and `src/newton_mcp/runtime/verifier.py` re-observes the world through the
capability's `read_tool`, evaluating the contract's structured `verification.condition`
(`src/newton_mcp/action/conditions.py`) before ever deciding to retry a non-idempotent action.
Mock-validated only -- no live actuator or live Newton credentials. See `docs/action-runtime.md`.

## Demo: a closed-loop smart-home testbed (mock-validated)

`examples/smart-home/` is a self-contained, runnable artefact for the full Direction A pipeline
above: `propose_action` → capability resolution → policy → approval → execution → verification,
against an in-process fake smart-home actuator (`fake_alice.py`) and a simulated kitchen. No
socket, no subprocess, no credential.

![Two mock runs of the smart-home demo: a verified success, and an AC that accepts the call but never cools the room, retried once and then escalated](docs/demo.gif)

The recording is the mock backend with the in-process fake actuator -- no live Newton account and
no real device. It is regenerated from `docs/demo.tape` with [VHS](https://github.com/charmbracelet/vhs).

```bash
uv run python examples/smart-home/demo.py --mock
```

Success path excerpt:

```
policy: decision=auto reason="matched rule 'ac-reduce-temperature'"
terminal state: SUCCEEDED
actuator tool calls: 1
read polls: 3
```

`--ac-offline` demonstrates the failure this project exists to catch -- a digitally successful
tool call (`{"accepted": true}`) with no physical effect, so the run ends `ESCALATED` rather than
`SUCCEEDED`:

```
uv run python examples/smart-home/demo.py --mock --ac-offline
...
terminal state: ESCALATED
actuator tool calls: 2
read polls: 20
```

The two actuator calls are the first attempt and the one retry allowed by the mock contract's
`retry_limit: 1` (the AC capability is idempotent); the read polls are the verifier's
`get_room_state` calls between and after them, counted separately.

Both excerpts are **mock-validated**. See `examples/smart-home/README.md` for the full capability
table, every flag and exit code, the `trace.jsonl` regeneration command, and why a real
smart-home server is not yet drivable by this runtime.

## What is confirmed vs. proposed

| | Source |
|---|---|
| `/query` request/response shape, model families, env variable names | Archetype public docs |
| Agents API (blueprints → bundles → runs, results/events) | Archetype public docs |
| This gateway's MCP tool mapping | this project |
| Physical Action Contract, policy engine | **this project's experimental proposal** |
| Lifecycle (`ActionState`, including `UNKNOWN`), JSONL audit trail, executor, verifier | **this project's experimental proposal** |
| Open questions for Archetype engineers | see `docs/archetype-integration.md` |

Nothing here reverse-engineers private endpoints or circumvents access controls.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Archetype's own terminology, Direction A vs. Direction B, and the lifecycle summary. |
| [`docs/action-runtime.md`](docs/action-runtime.md) | The MCP host, tool discovery, resolver, policy, approval, execution, verification and audit -- in detail, traced to `src/`. |
| [`docs/safety.md`](docs/safety.md) | The safety defaults tables, the invariants a reviewer can check, demo-safe actions, and known limitations. |
| [`docs/archetype-integration.md`](docs/archetype-integration.md) | What is confirmed from Archetype's public documentation versus what this project proposes, plus the open questions for an Archetype engineer. |
| [`docs/newton-api-notes.md`](docs/newton-api-notes.md) | Traceability notes for the `/query` and Files API fields `newton_analyze_image` sends. |
| [`examples/smart-home/README.md`](examples/smart-home/README.md) | The runnable, mock-validated closed-loop demo: capabilities, flags, exit codes, and why a real actuator is not drivable yet. |

## Development

```bash
uv sync --group dev
uv run pytest
```

Layout: `src/newton_mcp/newton/` (backend protocol, mock, real adapter) ·
`src/newton_mcp/action/` (contract, policy) · `src/newton_mcp/server.py` (MCP server) ·
`schemas/` · `examples/` · `docs/`.

## License

MIT
