# Guidance for coding agents working in this repo

- Python 3.12, `uv`, Pydantic v2, `mcp>=2` (note: v2 renamed FastMCP → `mcp.server.mcpserver.MCPServer`).
- Run `uv sync --group dev && uv run pytest` before and after changes. Keep tests green.
- Only use **publicly documented** Archetype behaviour (docs.archetypeai.app). Never invent endpoints,
  parameters or model ids. Env var names for the real backend are `ATAI_API_KEY` / `ATAI_API_ENDPOINT`.
- Mock output must stay clearly labelled (`backend: "mock"`, `[mock]` prefix). Never make it look real.
- Do not claim a live Newton integration works until it has been tested with real credentials.
- Prefer small, explicit code over frameworks. No Temporal/Kubernetes/DB in this repo unless an issue asks.
- `schemas/physical-action-contract.schema.json` is generated from `PhysicalActionContract`; regenerate it
  when the model changes (a test enforces sync).
- The Physical Action Contract and action runtime are *this project's proposal*, not Archetype's — keep
  wording in docs consistent with that.
