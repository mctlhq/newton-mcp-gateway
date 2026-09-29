"""Fixtures for `tests/runtime/`: fake MCP servers and `ClientFactory` doubles.

Every fixture here is subprocess-free and socket-free: fake servers connect
through `mcp`'s in-process transport (`Client(<MCPServer>)`), and the
hang/pagination doubles implement `SupportsListTools` directly with no
transport at all.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anyio
import pytest
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


@pytest.fixture
def deterministic_id_factory() -> Callable[[], str]:
    """A counter-based id_factory yielding `0000000000000001`, `0000000000000002`, ...

    16 hex characters, matching the default `secrets.token_hex(8)` shape, so
    assertions on generated ids (`obs-0000000000000001`, ...) can be literals.
    """
    counter = iter(range(1, 1_000_000))

    def factory() -> str:
        return f"{next(counter):016x}"

    return factory


@pytest.fixture
def fixed_now() -> datetime:
    """A fixed, aware UTC `datetime` for lifecycle/audit assertions."""
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def recording_handler(
    call_log: list[tuple[str, dict[str, Any]]],
    name: str,
    arg_names: Iterable[str] = (),
    result: Any = None,
) -> Callable[..., Any]:
    """A tool handler that appends `(name, kwargs)` to `call_log` and returns `result` (default `{}`).

    `arg_names` becomes the handler's advertised parameter names via an
    explicit `__signature__` override -- `MCPServer.add_tool` derives the
    tool's input schema from `inspect.signature`, which does not support a
    bare `**kwargs` handler, but keyword arguments still land in `kwargs` at
    call time regardless of the declared signature.
    """

    async def handler(**kwargs: Any) -> Any:
        call_log.append((name, kwargs))
        return {} if result is None else result

    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter(arg_name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Any)
            for arg_name in arg_names
        ]
    )
    handler.__name__ = name
    return handler


def scripted_handler(results: Iterable[Any]) -> Callable[[], Any]:
    """A read-tool handler that returns each of `results` in order, one per call.

    Each item is either a plain JSON-able value returned as the tool's
    result, or a zero-arg callable invoked for its side effect (e.g. to
    raise), so a scripted scenario can mix observations with failed polls.
    Raises `AssertionError` if called more times than `results` provides.
    """
    iterator = iter(results)

    async def handler() -> Any:
        try:
            item = next(iterator)
        except StopIteration:
            raise AssertionError("scripted_handler called more times than results were provided") from None
        if callable(item):
            return item()
        return item

    return handler


class DeterministicClock:
    """A fake monotonic clock paired with a `sleep` that advances it -- no real waiting.

    Used as the `Verifier`'s injected `clock`/`sleep` seam so a poll-loop test
    drives the deadline deterministically instead of sleeping in real time.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def deterministic_clock() -> DeterministicClock:
    return DeterministicClock()
