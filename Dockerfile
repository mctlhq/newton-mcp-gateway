# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first so this layer is cached across source-only changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# Now install the project itself.
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /usr/sbin/nologin newton

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
RUN chmod -R go-w /app

# HOST/PORT below are bare compatibility defaults, not NEWTON_MCP_HOST/NEWTON_MCP_PORT:
# the prefixed names out-rank bare ones, so setting them here would make every
# operator `docker run -e HOST=... / -e PORT=...` override a silent no-op. Override
# with `-e HOST=...` / `-e PORT=...` (as before) or `-e NEWTON_MCP_HOST=...` /
# `-e NEWTON_MCP_PORT=...` (preferred, wins over both).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NEWTON_BACKEND=mock \
    NEWTON_MCP_TRANSPORT=streamable-http \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

USER newton

ENTRYPOINT ["newton-mcp"]
