"""MCP server exposing Newton as a capability (Direction B).

Two tools on purpose. More is not better: each tool maps to a documented
/query pattern and returns structured, auditable output.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from newton_mcp import __version__
from newton_mcp.config import Settings
from newton_mcp.newton.api import build_backend
from newton_mcp.newton.models import DataEvent, NewtonQueryRequest
from newton_mcp.newton.protocol import NewtonBackend


@dataclass
class AppState:
    settings: Settings
    backend: NewtonBackend


def create_server(settings: Settings | None = None, backend: NewtonBackend | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    backend = backend or build_backend(settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppState]:
        try:
            yield AppState(settings=settings, backend=backend)
        finally:
            await backend.aclose()

    server = MCPServer(
        name="newton-mcp-gateway",
        version=__version__,
        instructions=(
            "Experimental bridge to Archetype AI Newton, a foundation model for physical sensor data. "
            f"Backend: {backend.name}. Mock results are labelled and are not real inference."
        ),
        lifespan=lifespan,
    )

    def _state(ctx: Context) -> AppState:
        return ctx.request_context.lifespan_context  # type: ignore[return-value]

    @server.tool(
        name="newton_query",
        description=(
            "Ask Newton (text-reasoning model) a natural-language question about physical-world data. "
            "Ground it with inline text/JSON events or previously uploaded file_ids. "
            "Use system_prompt to force structured JSON output."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def newton_query(
        ctx: Context,
        query: str,
        system_prompt: str = "",
        text_events: list[str] | None = None,
        json_events: list[str] | None = None,
        file_ids: list[str] | None = None,
        max_new_tokens: int = 400,
        model: str | None = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        events = [DataEvent.text(t) for t in (text_events or [])]
        events += [DataEvent(type="data.json", event_data={"contents": j}) for j in (json_events or [])]
        request = NewtonQueryRequest(
            model=model or state.settings.text_model,
            query=query,
            system_prompt=system_prompt,
            instruction_prompt=system_prompt,
            events=events,
            file_ids=file_ids or [],
            max_new_tokens=max_new_tokens,
        )
        result = await state.backend.query(request)
        return result.model_dump(exclude={"raw"})

    @server.tool(
        name="newton_embed_timeseries",
        description=(
            "Encode a sensor window with the Newton Omega encoder. Input is channel-first: "
            "outer list = channels, inner lists = samples. Returns one 768-dim embedding per channel. "
            "Leave normalize=false unless cross-window amplitude is irrelevant."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def newton_embed_timeseries(
        ctx: Context,
        channels: list[list[float]],
        normalize: bool = False,
        model: str | None = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        if not channels or any(len(c) == 0 for c in channels):
            raise ValueError("channels must be a non-empty list of non-empty sample lists")
        request = NewtonQueryRequest(
            model=model or state.settings.omega_model,
            query="",
            events=[DataEvent.numeric_array(channels)],
            normalize_input=normalize,
        )
        result = await state.backend.query(request)
        payload = result.model_dump(exclude={"raw"})
        payload["embedding_dims"] = [len(v) for v in result.outputs]
        return payload

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
