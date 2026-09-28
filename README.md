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
> with an authorized `ATAI_API_KEY`.
> This project is not affiliated with or endorsed by Archetype AI.

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

## Tools

Deliberately few. Each maps to a documented Direct Query pattern.

| Tool | Newton model family | What it does |
|---|---|---|
| `newton_query` | Newton C (text / image / video reasoning) | Natural-language question grounded in inline text/JSON events or uploaded `file_ids`. Use `system_prompt` to force structured JSON. |
| `newton_embed_timeseries` | Omega encoder | Channel-first sensor window → one 768-dim embedding per channel. |

Planned (see issues): image analysis via the Files API, running Newton Agent bundles
(`osm`, `anomaly-discovery`, `rare-event-detection`, `task-verification`) and paging their results.

Mock results are labelled `backend: "mock"` and text outputs start with `[mock]`. They are never
presented as real Newton output.

## Direction A: the Physical Action Contract (proposal)

A successful MCP tool call is not a successful physical action. `set_temperature(23)` can return
`200 OK` while the AC is offline, the wrong zone was changed, or the room simply does not cool.
Physical actions need **policy, approval and outcome verification** on top of ordinary tool calling.

This project proposes a tool-independent **Physical Action Contract** (`schemas/`, v0.1):

```json
{
  "goal": "reduce_room_temperature",
  "reason": "The occupied kitchen reached 29.4 C",
  "confidence": 0.96,
  "target": { "type": "environment", "location": "kitchen" },
  "constraints": { "desired_temperature_c": 23, "minimum_temperature_c": 20, "maximum_temperature_c": 25 },
  "risk": "low",
  "verification": { "condition": "temperature_c <= 24", "timeout_seconds": 600 }
}
```

Newton says *what* should happen. The MCP environment knows *how*. A small action runtime sits in
between:

```
contract → capability match (MCP tool discovery) → policy (auto / confirm / deny)
        → human approval if required → execute → observe → verify → succeed / retry / escalate
```

The contract can be produced by a Newton `/query` with a strict JSON system prompt — a documented
usage pattern — or by a Newton Agent's output. The policy engine in `newton_mcp/action/` is a
deterministic first cut. The runtime, capability resolver and verifier are the next phases.

The first real actuator testbed is a smart-home MCP server (lights, HVAC, speaker), chosen because it
is real hardware with benign, reversible actions. The interface itself is domain-independent:
Home Assistant, BMS, OPC-UA, ROS or enterprise workflow MCP servers plug in the same way.

## What is confirmed vs. proposed

| | Source |
|---|---|
| `/query` request/response shape, model families, env variable names | Archetype public docs |
| Agents API (blueprints → bundles → runs, results/events) | Archetype public docs |
| This gateway's MCP tool mapping | this project |
| Physical Action Contract, policy engine, action runtime | **this project's experimental proposal** |

Nothing here reverse-engineers private endpoints or circumvents access controls.

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
