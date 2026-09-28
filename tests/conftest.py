from types import SimpleNamespace

import pytest

from newton_mcp.config import Settings
from newton_mcp.newton.mock import MockNewtonBackend
from newton_mcp.newton.models import ImageUpload, NewtonQueryRequest, NewtonQueryResult, UploadedFile
from newton_mcp.newton.protocol import NewtonBackend
from newton_mcp.server import AppState, create_server


@pytest.fixture
def mock_backend() -> MockNewtonBackend:
    return MockNewtonBackend()


class ScriptedNewtonBackend:
    """A `NewtonBackend` driven by a queue of scripted `NewtonQueryResult` outputs.

    Each entry in `outputs` is passed to `NewtonQueryResult(backend="api",
    query_id=..., model=request.model, **entry)`, so a test can script
    `{"status": "completed", "outputs": [...]}` or `{"status": "failed",
    "outputs": [...], "error": "..."}`. Raises `AssertionError` if queried
    more times than scripted, so an over-eager retry is caught immediately.
    """

    name = "api"

    def __init__(self, outputs: list[dict]) -> None:
        self._outputs = list(outputs)
        self.requests: list[NewtonQueryRequest] = []

    async def query(self, request: NewtonQueryRequest) -> NewtonQueryResult:
        self.requests.append(request)
        if not self._outputs:
            raise AssertionError(
                f"ScriptedNewtonBackend queried {len(self.requests)} times but only "
                f"{len(self.requests) - 1} outputs were scripted"
            )
        entry = self._outputs.pop(0)
        return NewtonQueryResult(
            backend="api",
            query_id=f"scripted-{len(self.requests):06d}",
            model=request.model,
            inference_time_sec=0.0,
            **entry,
        )

    async def upload_image(self, image: ImageUpload) -> UploadedFile:
        raise NotImplementedError("ScriptedNewtonBackend does not support upload_image")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def server(mock_backend):
    return create_server(Settings(), backend=mock_backend)


async def call_tool(server, settings: Settings, backend: NewtonBackend, name: str, arguments: dict):
    """Call a registered tool with a real lifespan context.

    `MCPServer.call_tool` builds its `Context` without a request context (it is
    meant for tool-to-tool calls that don't need `ctx.request_context`), so
    tools that read `ctx.request_context.lifespan_context` -- every tool in
    this server -- can't be exercised through it directly. This constructs the
    same `AppState` the real lifespan would yield, using the exact `settings`
    and `backend` the test built the server with, and hands it to the tool
    through a minimal stand-in carrying only the one attribute `_state()` reads.
    """
    from mcp.server.mcpserver import Context

    tool = server._tool_manager.get_tool(name)
    assert tool is not None, f"tool {name!r} is not registered"
    ctx = Context(
        request_context=SimpleNamespace(lifespan_context=AppState(settings=settings, backend=backend)),
        mcp_server=server,
    )
    return await tool.run(arguments, ctx, convert_result=True)
