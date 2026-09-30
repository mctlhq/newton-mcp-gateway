"""MCP host: connects to configured servers as a client and discovers their tools.

This module issues no MCP request other than the connection handshake and
`list_tools` -- in particular it never calls `call_tool`. Execution, policy
and approval are out of scope for this proposal; see docs/action-runtime.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal, Protocol

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation, ListToolsResult
from pydantic import BaseModel, ConfigDict

from newton_mcp.runtime.auth import MissingAuthSecret, resolve_auth_header
from newton_mcp.runtime.config import CapabilityConfig, HttpTransport, RuntimeConfig, ServerConfig, StdioTransport

if TYPE_CHECKING:
    import httpx2

DEFAULT_SERVER_TIMEOUT_SECONDS = 10.0
MAX_TOOL_PAGES = 1000
_MAX_DETAIL_CHARS = 500

#: `default_client_factory`'s authenticated branch builds the `httpx2.AsyncClient`
#: through this seam so a test can inject an ASGI-transport-backed client
#: (socket-free) without patching the SDK import. Keyword-only in
#: `default_client_factory` so its call signature stays `Callable[[ServerConfig], ...]`.
HttpClientBuilder = Callable[[dict[str, str]], "httpx2.AsyncClient"]


def _default_http_client_builder(headers: dict[str, str]) -> "httpx2.AsyncClient":
    # Imported lazily so `httpx2` (a transitive dependency via `mcp`) is only
    # required for an authenticated streamable-http server, and so this
    # module's top-level `httpx2` reference stays TYPE_CHECKING-only.
    from mcp.shared._httpx_utils import create_mcp_http_client

    return create_mcp_http_client(headers=headers)


@asynccontextmanager
async def _authenticated_http_client(
    url: str, headers: dict[str, str], builder: HttpClientBuilder
) -> AsyncIterator[SupportsListTools]:
    """Build an authenticated streamable-http `Client`, owning the `httpx2.AsyncClient`'s lifecycle.

    `mcp.client.streamable_http.streamable_http_client` only enters a
    *caller-supplied* `http_client` onto its own exit stack when it created
    that client itself -- it will not close one we hand it. Nesting the
    `async with` here instead makes this wrapper the owner: `builder(headers)`
    is closed on the success path and on any exception path alike, including
    cancellation, because that is what an `async with` block already
    guarantees -- no extra `try`/`finally` is needed for that half of the
    contract.

    No exception is caught or reclassified here. `CapabilityCatalog.refresh()`
    is what turns a connect/list failure for an authenticated server into a
    class-only diagnostic (`_catalog_problem_detail`); `Executor.execute()`
    already does the same for a call failure (`_classify_call_failure`).
    Sanitizing here instead would also have to catch -- and so could not tell
    apart from an SDK failure -- an exception a caller's own code inside the
    `async with self._client_factory(server) as client:` block deliberately
    raises, which must propagate untouched.
    """
    async with builder(headers) as http_client:  # we own it: the SDK will not close it
        async with Client(streamable_http_client(url, http_client=http_client)) as client:
            yield client  # type: ignore[misc]


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


def default_client_factory(
    server: ServerConfig, *, http_client_builder: HttpClientBuilder = _default_http_client_builder
) -> AbstractAsyncContextManager[SupportsListTools]:
    """Map `StdioTransport` -> subprocess `Client`, `HttpTransport` -> streamable-http `Client`.

    This is the seam a pooled/long-lived implementation would later replace; tests pass
    their own factory so they spawn no subprocess and open no socket. `mcp.Client` speaks
    both `list_tools` and `call_tool`, so `runtime/executor.py` reuses this same factory
    rather than duplicating the transport mapping -- there is exactly one place that maps
    a transport to a client.

    `http_client_builder` is keyword-only with a default, so this still satisfies
    `ClientFactory = Callable[[ServerConfig], ...]` and every existing caller
    (`CapabilityCatalog`, `Executor`, `Verifier`) needs no change. It exists so a test
    can inject an ASGI-transport-backed `httpx2.AsyncClient` builder, socket-free.

    An `HttpTransport` with no declared `auth` returns `Client(transport.url)`
    byte-for-byte unchanged from before this seam existed. One with `auth`
    resolves the header (raising `MissingAuthSecret` -- naming the variable
    and the server, never opening any transport -- if it is unset or blank)
    and returns `_authenticated_http_client`, which owns the resulting
    `httpx2.AsyncClient`'s lifecycle.
    """
    transport = server.transport
    if isinstance(transport, StdioTransport):
        target = StdioServerParameters(
            command=transport.command,
            args=list(transport.args),
            env=dict(transport.env) or None,
        )
        return Client(target)  # type: ignore[return-value]
    elif isinstance(transport, HttpTransport):
        if transport.auth is None:
            return Client(transport.url)  # type: ignore[return-value]
        header_name, header_value = resolve_auth_header(transport.auth, server_name=server.name)
        return _authenticated_http_client(transport.url, {header_name: header_value}, http_client_builder)
    else:  # pragma: no cover - the discriminated union covers every case
        raise ValueError(f"unsupported transport {transport!r}")


#: Backward-compatible alias for the previous private name.
_default_client_factory = default_client_factory


def _truncate(text: str) -> str:
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return text[: _MAX_DETAIL_CHARS - 3] + "..."


def _declares_auth(server: ServerConfig) -> bool:
    transport = server.transport
    return isinstance(transport, HttpTransport) and transport.auth is not None


def _exception_class_summary(exc: BaseException) -> str:
    """A class-only description of `exc`, recursing into an `ExceptionGroup`'s members.

    Never touches `str(exc)`/`repr(exc)` -- only `type(...).__name__` at every
    level -- so a nested exception group from an authenticated connect/list/
    call/close failure cannot smuggle a resolved header value out through a
    member exception's message.
    """
    if isinstance(exc, BaseExceptionGroup):
        inner = ", ".join(_exception_class_summary(sub) for sub in exc.exceptions)
        return f"{type(exc).__name__}[{inner}]"
    return type(exc).__name__


def _catalog_problem_detail(server: ServerConfig, exc: Exception) -> str:
    """A safe `CatalogProblem.detail` for a `server_unavailable` failure.

    For a server that declares `auth`, a raw `repr(exc)` is not safe: an
    httpx2/anyio transport exception (or a nested `ExceptionGroup` of them)
    can embed request state, and the resolved auth header was carried on
    that exact request. `MissingAuthSecret`'s own message is the one
    exception exempted from this -- it already names only the validated
    variable and server, never a value, by construction (`runtime/auth.py`)
    -- every other exception for an authenticated server is reduced to its
    exception class (recursing into an `ExceptionGroup`). A server with no
    `auth` keeps today's `repr(exc)` behaviour unchanged. Sanitization
    happens here, before `_truncate()`, and never re-reads the environment:
    the value that failed may have already rotated.
    """
    if isinstance(exc, MissingAuthSecret):
        return str(exc)
    if _declares_auth(server):
        return f"transport failure ({_exception_class_summary(exc)})"
    return repr(exc)


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
        self._client_factory = client_factory or default_client_factory
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
                        detail=_truncate(_catalog_problem_detail(server, exc)),
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
