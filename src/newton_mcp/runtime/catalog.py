"""MCP host: connects to configured servers as a client and discovers their tools.

This module issues no MCP request other than the connection handshake and
`list_tools` -- in particular it never calls `call_tool`. Execution, policy
and approval are out of scope for this proposal; see docs/action-runtime.md.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

import anyio
from mcp import Client, StdioServerParameters
from mcp_types import Implementation, ListToolsResult
from pydantic import BaseModel, ConfigDict

from newton_mcp.runtime.config import CapabilityConfig, HttpTransport, RuntimeConfig, ServerConfig, StdioTransport

DEFAULT_SERVER_TIMEOUT_SECONDS = 10.0
MAX_TOOL_PAGES = 1000
_MAX_DETAIL_CHARS = 500


class SupportsListTools(Protocol):
    """The slice of `mcp.Client` the catalog needs, once entered as a context manager."""

    server_info: Implementation | None

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult: ...


ClientFactory = Callable[[ServerConfig], AbstractAsyncContextManager[SupportsListTools]]


class DiscoveredTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None = None
    input_schema: dict[str, Any]
    read_only_hint: bool | None = None


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: CapabilityConfig
    server: ServerConfig
    tool: DiscoveredTool
    read_tool: DiscoveredTool | None = None


class CatalogProblem(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["server_unavailable", "tool_missing", "read_tool_missing"]
    server: str
    tool: str | None = None
    detail: str


class ObservedServerInfo(BaseModel):
    """The server's observed MCP `serverInfo`, recorded as metadata only.

    Never used for filtering, ranking or identity -- `CandidateAction.server_identity`
    stays the configured identity. See the owner amendment in requirements.md.
    """

    model_config = ConfigDict(frozen=True)

    server: str
    name: str
    version: str


class CatalogSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[CatalogEntry, ...] = ()
    problems: tuple[CatalogProblem, ...] = ()
    server_info: tuple[ObservedServerInfo, ...] = ()


_EMPTY_SNAPSHOT = CatalogSnapshot()


def _default_client_factory(server: ServerConfig) -> AbstractAsyncContextManager[SupportsListTools]:
    """Map `StdioTransport` -> subprocess `Client`, `HttpTransport` -> streamable-http `Client`.

    This is the seam a pooled/long-lived implementation would later replace; tests pass
    their own factory so they spawn no subprocess and open no socket.
    """
    transport = server.transport
    target: str | StdioServerParameters
    if isinstance(transport, StdioTransport):
        target = StdioServerParameters(
            command=transport.command,
            args=list(transport.args),
            env=dict(transport.env) or None,
        )
    elif isinstance(transport, HttpTransport):
        target = transport.url
    else:  # pragma: no cover - the discriminated union covers every case
        raise ValueError(f"unsupported transport {transport!r}")
    return Client(target)  # type: ignore[return-value]


def _truncate(text: str) -> str:
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return text[: _MAX_DETAIL_CHARS - 3] + "..."


class CapabilityCatalog:
    """The gateway's MCP host: discovery only, no `call_tool`, no lifecycle."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        client_factory: ClientFactory | None = None,
        server_timeout_seconds: float = DEFAULT_SERVER_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._server_timeout_seconds = server_timeout_seconds
        self._snapshot: CatalogSnapshot = _EMPTY_SNAPSHOT

    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def snapshot(self) -> CatalogSnapshot:
        """The last successful `refresh()` result, or an empty snapshot before the first one."""
        return self._snapshot

    async def refresh(self) -> CatalogSnapshot:
        """Connect to every configured server, discover tools, and replace the snapshot atomically.

        Cancellation of this coroutine propagates: only `Exception` (including
        `ExceptionGroup`) is caught per server, never `BaseException` or
        `BaseExceptionGroup`. A cancelled refresh never assigns, so the previous
        snapshot stays in place.
        """
        entries: list[CatalogEntry] = []
        problems: list[CatalogProblem] = []
        server_infos: list[ObservedServerInfo] = []

        for server in self._config.servers:
            capabilities_for_server = [c for c in self._config.capabilities if c.server == server.name]

            try:
                with anyio.fail_after(self._server_timeout_seconds):
                    discovered, observed_info = await self._discover_server(server)
            except Exception as exc:
                problems.append(
                    CatalogProblem(
                        kind="server_unavailable",
                        server=server.name,
                        tool=None,
                        detail=_truncate(repr(exc)),
                    )
                )
                continue

            if observed_info is not None:
                server_infos.append(observed_info)

            for capability in capabilities_for_server:
                tool = discovered.get(capability.tool)
                if tool is None:
                    problems.append(
                        CatalogProblem(
                            kind="tool_missing",
                            server=server.name,
                            tool=capability.tool,
                            detail=f"tool {capability.tool!r} not found in server {server.name!r}'s listing",
                        )
                    )
                    continue

                read_tool: DiscoveredTool | None = None
                if capability.read_tool is not None:
                    read_tool = discovered.get(capability.read_tool)
                    if read_tool is None:
                        problems.append(
                            CatalogProblem(
                                kind="read_tool_missing",
                                server=server.name,
                                tool=capability.read_tool,
                                detail=(
                                    f"read_tool {capability.read_tool!r} not found in server "
                                    f"{server.name!r}'s listing"
                                ),
                            )
                        )

                entries.append(
                    CatalogEntry(capability=capability, server=server, tool=tool, read_tool=read_tool)
                )

        self._snapshot = CatalogSnapshot(
            entries=tuple(entries),
            problems=tuple(problems),
            server_info=tuple(server_infos),
        )
        return self._snapshot

    async def _discover_server(
        self, server: ServerConfig
    ) -> tuple[dict[str, DiscoveredTool], ObservedServerInfo | None]:
        """Connect, read `serverInfo`, and page `list_tools` to exhaustion for one server.

        The caller wraps this whole call in one `anyio.fail_after` scope, so the
        deadline covers connect, the initialize handshake, and every page.
        """
        async with self._client_factory(server) as client:
            observed_info: ObservedServerInfo | None = None
            server_info = client.server_info
            if server_info is not None:
                observed_info = ObservedServerInfo(
                    server=server.name, name=server_info.name, version=server_info.version
                )

            discovered: dict[str, DiscoveredTool] = {}
            cursor: str | None = None
            for _ in range(MAX_TOOL_PAGES):
                result = await client.list_tools(cursor=cursor)
                for tool in result.tools:
                    discovered[tool.name] = DiscoveredTool(
                        name=tool.name,
                        description=tool.description,
                        input_schema=tool.input_schema,
                        read_only_hint=tool.annotations.read_only_hint if tool.annotations else None,
                    )
                cursor = result.next_cursor
                if cursor is None:
                    return discovered, observed_info

            raise RuntimeError(
                f"tool listing for server {server.name!r} did not terminate within MAX_TOOL_PAGES={MAX_TOOL_PAGES}"
            )
