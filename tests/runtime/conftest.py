"""Fixtures for `tests/runtime/`: fake MCP servers and `ClientFactory` doubles.

Every fixture here is subprocess-free and socket-free: fake servers connect
through `mcp`'s in-process transport (`Client(<MCPServer>)`), and the
hang/pagination doubles implement `SupportsListTools` directly with no
transport at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from mcp_types import Implementation, ListToolsResult, Tool

from newton_mcp.runtime.catalog import ClientFactory, SupportsListTools
from newton_mcp.runtime.config import ServerConfig


@dataclass
class FakeToolSpec:
    name: str
    handler: Callable[..., Any]
    annotations: ToolAnnotations | None = None
    description: str | None = None


def build_fake_server(name: str, tools: Iterable[FakeToolSpec]) -> MCPServer:
    """An in-process `MCPServer` whose tool bodies record any invocation.

    Wrap the return value with `mcp.Client(server)` to connect to it in-process
    via `InMemoryTransport` -- no subprocess, no socket.
    """
    server = MCPServer(name=name, version="0.0.0-fake")
    for spec in tools:
        server.add_tool(spec.handler, name=spec.name, description=spec.description, annotations=spec.annotations)
    return server


def in_memory_factory(servers_by_name: dict[str, MCPServer]) -> ClientFactory:
    """A `ClientFactory` that connects in-process to a fake server by config name.

    A server name absent from `servers_by_name` raises inside the factory
    (before any connection is attempted), so the catalog records that server
    as `server_unavailable`.
    """

    def factory(server: ServerConfig) -> AbstractAsyncContextManager[SupportsListTools]:
        fake = servers_by_name[server.name]
        return Client(fake)  # type: ignore[return-value]

    return factory


class _FakeSession:
    """A minimal `SupportsListTools` double with no transport at all.

    Used where a real `MCPServer` cannot express the behaviour under test
    (hanging mid-listing, or a controlled multi-page listing). `list_tools`
    is an async callable so it can block forever as well as return.
    """

    def __init__(
        self,
        *,
        list_tools: Callable[[str | None], Any],
        server_info: Implementation | None = None,
    ) -> None:
        self._list_tools = list_tools
        self.server_info = server_info

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        return await self._list_tools(cursor)


def hanging_factory(*, hang_on_connect: bool = False) -> ClientFactory:
    """A `ClientFactory` whose session never answers `list_tools` (or hangs before connect).

    `hang_on_connect=True` blocks inside `__aenter__`, before any request --
    used to prove the timeout also bounds connect/initialize, not just paging.
    """

    async def _hang(_cursor: str | None) -> ListToolsResult:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")  # pragma: no cover

    @asynccontextmanager
    async def factory(_server: ServerConfig) -> AsyncIterator[SupportsListTools]:
        if hang_on_connect:
            await anyio.sleep_forever()
        yield _FakeSession(list_tools=_hang)

    return factory


def hanging_after_first_page_factory(first_page: list[Tool]) -> ClientFactory:
    """A `ClientFactory` whose session answers page one, then hangs on page two."""

    async def _list_tools(cursor: str | None) -> ListToolsResult:
        if cursor is None:
            return ListToolsResult(tools=first_page, next_cursor="page-2")
        await anyio.sleep_forever()
        raise AssertionError("unreachable")  # pragma: no cover

    @asynccontextmanager
    async def factory(_server: ServerConfig) -> AsyncIterator[SupportsListTools]:
        yield _FakeSession(list_tools=_list_tools)

    return factory


def paginated_factory(pages: list[list[Tool]]) -> ClientFactory:
    """A `ClientFactory` that serves `pages` of tools across successive `list_tools` calls."""

    def make_list_tools() -> Callable[[str | None], Any]:
        remaining = list(enumerate(pages))

        async def list_tools(_cursor: str | None) -> ListToolsResult:
            if not remaining:
                raise AssertionError("list_tools called after the last page was already served")
            index, page = remaining.pop(0)
            next_cursor = str(index + 1) if remaining else None
            return ListToolsResult(tools=page, next_cursor=next_cursor)

        return list_tools

    @asynccontextmanager
    async def factory(_server: ServerConfig) -> AsyncIterator[SupportsListTools]:
        yield _FakeSession(list_tools=make_list_tools())

    return factory


def raising_factory(exc: BaseException) -> ClientFactory:
    """A `ClientFactory` whose `__aenter__` raises `exc` immediately."""

    @asynccontextmanager
    async def factory(_server: ServerConfig) -> AsyncIterator[SupportsListTools]:
        raise exc
        yield  # pragma: no cover - unreachable, keeps this an async generator

    return factory


def task_group_raising_factory(message: str) -> ClientFactory:
    """A `ClientFactory` whose connect fails inside a nested `anyio` task group.

    An ordinary failure inside a task group's child task surfaces at the
    `async with` exit as an `ExceptionGroup` (not a bare exception), exactly
    like the anyio task groups a real stdio/http connect path uses.
    """

    @asynccontextmanager
    async def factory(_server: ServerConfig) -> AsyncIterator[SupportsListTools]:
        async def _boom() -> None:
            raise RuntimeError(message)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_boom)
        yield _FakeSession(list_tools=lambda _cursor: _empty())  # pragma: no cover - unreachable

    return factory


async def _empty() -> ListToolsResult:
    return ListToolsResult(tools=[])
