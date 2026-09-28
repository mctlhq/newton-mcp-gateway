from types import SimpleNamespace

import pytest

from newton_mcp.config import Settings
from newton_mcp.newton.mock import MockNewtonBackend
from newton_mcp.newton.protocol import NewtonBackend
from newton_mcp.server import AppState, create_server


@pytest.fixture
def mock_backend() -> MockNewtonBackend:
    return MockNewtonBackend()


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
