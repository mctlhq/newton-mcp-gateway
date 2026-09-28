# Contributing

## Setup

```bash
uv sync --group dev
```

## Running tests

```bash
uv run pytest
```

## Updating the lockfile

`uv.lock` is committed and CI installs with `uv sync --locked --group dev`, so it fails
loudly if the lock drifts from `pyproject.toml` instead of silently re-resolving.

- Refresh the whole lock after editing `pyproject.toml`: `uv lock`
- Bump a single dependency: `uv lock --upgrade-package <name>`

Either way, run `uv sync --locked --group dev && uv run pytest -q` afterwards and commit
the updated `uv.lock` alongside your change.

## Transports

The gateway speaks two transports, selected by `NEWTON_MCP_TRANSPORT`:

| Value | Default | Notes |
|---|---|---|
| `stdio` | yes | For local MCP hosts (Claude Desktop, Claude Code). |
| `streamable-http` | no | Serves `/mcp` over HTTP; binds `HOST`/`PORT` (default `127.0.0.1:8000`). |

The Docker image overrides these at the image level (`NEWTON_MCP_TRANSPORT=streamable-http`,
`HOST=0.0.0.0`, `PORT=8000`) so `docker run -p 8000:8000 <image>` serves HTTP out of the box,
while a bare `uv run newton-mcp` outside the container still speaks `stdio`.

## Regenerating the JSON schema

`schemas/physical-action-contract.schema.json` is generated from the `PhysicalActionContract`
Pydantic model. `tests/test_action_contract.py` asserts the committed file is byte-for-byte
equal to `PhysicalActionContract.model_json_schema()`, so the schema must be regenerated
whenever that model changes:

```bash
uv run python -c "
import json
from newton_mcp.action.contract import PhysicalActionContract
print(json.dumps(PhysicalActionContract.model_json_schema(), indent=2))
" > schemas/physical-action-contract.schema.json
```

(Adjust the import path above if `PhysicalActionContract` has moved.) Run `uv run pytest`
afterwards to confirm the file and the model agree.

## Hard rules

See [`AGENTS.md`](./AGENTS.md) for the project's non-negotiable rules (mock labelling,
publicly-documented Archetype behaviour only, the Physical Action Contract's status as
this project's proposal, and so on). This file only covers day-to-day setup; `AGENTS.md`
is the source of truth for everything else.
