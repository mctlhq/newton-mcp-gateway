"""End-to-end, socket-free tests for an authenticated streamable-http transport.

Drives `default_client_factory()`'s authenticated branch against a real
`mcp.server.mcpserver.MCPServer` through `httpx2.ASGITransport` -- no
subprocess, no socket -- so the header is proven to reach an actual MCP
server implementation's request headers, not just a hand-written double.
See `mctlhq/newton-mcp-gateway#28`, design.md and tasks.md (T7-T11).

`MCPServer.streamable_http_app()` wires `StreamableHTTPSessionManager.run()`
in as the Starlette app's *lifespan* context manager -- without entering it,
every request 500s with "Task group is not initialized." `_running_fake_app`
enters that context manager directly (`app.router.lifespan_context(app)`)
around each test's calls, the same thing the ASGI lifespan protocol would do.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from newton_mcp.action.approval import create_approval
from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
from newton_mcp.runtime.audit import MemoryAuditSink
from newton_mcp.runtime.catalog import CapabilityCatalog, HttpClientBuilder, default_client_factory
from newton_mcp.runtime.config import (
    CapabilityConfig,
    HttpTransport,
    RuntimeConfig,
    ServerConfig,
    StdioTransport,
    TargetMatch,
)
from newton_mcp.runtime.executor import Executor, run_action
from newton_mcp.runtime.lifecycle import ActionState, new_action_record, transition
from newton_mcp.runtime.resolver import CandidateAction
from newton_mcp.runtime.verifier import Verifier

from .conftest import FakeToolSpec, build_fake_server, in_memory_factory

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SENTINEL = "sk-live-super-secret-DO-NOT-LEAK-0123456789"  # noqa: S105 - test sentinel, not a real credential
AUTH_ENV_VAR = "ALICE_MCP_TOKEN"
SERVER_URL = "http://localhost:8080/mcp"


def _auth_server(name: str = "alice", url: str = SERVER_URL) -> ServerConfig:
    return ServerConfig(
        name=name,
        transport=HttpTransport(
            kind="streamable-http",
            url=url,
            auth={"header": "Authorization", "scheme": "Bearer", "env": AUTH_ENV_VAR},
        ),
    )


def _recording_asgi_app(app: Any, recorded: list[dict[str, str]]) -> Any:
    """Wrap `app` to record every inbound HTTP request's (lower-cased) headers."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            recorded.append(headers)
        await app(scope, receive, send)

    return wrapped


def _asgi_http_client_builder(app: Any) -> HttpClientBuilder:
    def builder(headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)

    return builder


@asynccontextmanager
async def _running_fake_app(recorded: list[dict[str, str]]) -> AsyncIterator[Any]:
    """A real, *running* in-process `MCPServer` ASGI app: one action tool, one read tool."""
    server = MCPServer(name="alice", version="0.0.0-fake")

    async def ping(value: str) -> dict:
        return {"ok": True, "value": value}

    server.add_tool(ping, name="ping", description="Echo a value. Idempotent absolute set.")

    async def read_ping() -> dict:
        return {"ok": True}

    server.add_tool(
        read_ping,
        name="read_ping",
        description="Read-only.",
        annotations=ToolAnnotations(read_only_hint=True),
    )

    app = server.streamable_http_app()
    async with app.router.lifespan_context(app):
        yield _recording_asgi_app(app, recorded)


def _capability(*, read_tool: str | None = "read_ping") -> CapabilityConfig:
    return CapabilityConfig(
        server="alice",
        tool="ping",
        goal_prefixes=("do_thing",),
        target=TargetMatch(type="environment"),
        arguments={"value": "hi"},
        read_tool=read_tool,
        read_arguments={},
        idempotent=True,
    )


def _candidate(server: ServerConfig, *, read_tool: str | None = "read_ping") -> CandidateAction:
    return CandidateAction(
        server_identity=server.resolved_identity,
        server_binding_identity=server.binding_identity,
        tool_name="ping",
        args={"value": "hi"},
        read_tool=read_tool,
        read_args={},
        idempotent=True,
        score=1.0,
        why="test",
    )


def _contains_exception(exc: BaseException, exc_type: type[BaseException]) -> bool:
    """Whether `exc`, or any exception nested inside a `BaseExceptionGroup`, is `exc_type`."""
    if isinstance(exc, exc_type):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_exception(sub, exc_type) for sub in exc.exceptions)
    return False


def _authorized_record() -> Any:
    record = new_action_record(now=NOW)
    return transition(record, ActionState.AUTHORIZED, "policy decided auto", now=NOW)


def _approval(candidate: CandidateAction, record: Any) -> Any:
    return create_approval(
        candidate,
        action_id=record.action_id,
        policy_version="policy.v1",
        approved_by="operator@example.com",
        approved_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        approval_id="approval-1",
    )


# ---------------------------------------------------------------------------
# T7: the header reaches connect, list_tools AND call_tool
# ---------------------------------------------------------------------------


async def test_auth_header_reaches_connect_list_tools_and_call_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    recorded: list[dict[str, str]] = []

    async with _running_fake_app(recorded) as app:
        factory = functools.partial(default_client_factory, http_client_builder=_asgi_http_client_builder(app))

        server = _auth_server()
        config = RuntimeConfig(servers=(server,), capabilities=(_capability(),))
        catalog = CapabilityCatalog(config, client_factory=factory)

        snapshot = await catalog.refresh()
        assert not snapshot.problems
        assert any(entry.tool.name == "ping" for entry in snapshot.entries)
        assert recorded, "connect/list_tools should have produced at least one recorded request"
        assert all(h.get("authorization") == f"Bearer {SENTINEL}" for h in recorded)

        recorded.clear()
        candidate = _candidate(server)
        record = _authorized_record()
        approval = _approval(candidate, record)
        executor = Executor(catalog, client_factory=factory, sink=MemoryAuditSink())

        record, outcome = await executor.execute(
            candidate, record, approval=approval, policy_version="policy.v1", now=NOW
        )
        assert outcome.state == ActionState.EXECUTED
        assert recorded, "call_tool should have produced at least one recorded request"
        assert all(h.get("authorization") == f"Bearer {SENTINEL}" for h in recorded)


# ---------------------------------------------------------------------------
# T8: a full refresh + approval + run_action cycle leaks the sentinel nowhere
# ---------------------------------------------------------------------------


async def test_full_cycle_leaks_sentinel_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    recorded: list[dict[str, str]] = []

    async with _running_fake_app(recorded) as app:
        factory = functools.partial(default_client_factory, http_client_builder=_asgi_http_client_builder(app))

        server = _auth_server()
        config = RuntimeConfig(servers=(server,), capabilities=(_capability(),))
        catalog = CapabilityCatalog(config, client_factory=factory)
        await catalog.refresh()

        candidate = _candidate(server)
        record = _authorized_record()
        approval = _approval(candidate, record)

        sink = MemoryAuditSink()
        executor = Executor(catalog, client_factory=factory, sink=sink)
        verifier = Verifier(catalog, client_factory=factory, sink=sink, poll_interval_seconds=0.01)
        contract = PhysicalActionContract(
            goal="do_thing",
            reason="test",
            target=Target(type="environment", location="kitchen"),
            risk=Risk.LOW,
            verification=Verification(
                condition={"path": "ok", "op": "eq", "value": True}, timeout_seconds=5, retry_limit=0
            ),
        )

        record, final_state = await run_action(
            candidate,
            contract,
            record,
            approval=approval,
            policy_version="policy.v1",
            executor=executor,
            verifier=verifier,
            now_fn=lambda: NOW,
        )
        assert final_state == ActionState.SUCCEEDED

        haystack_parts = [
            server.transport_fingerprint,
            server.binding_identity,
            candidate.server_binding_identity,
            approval.model_dump_json(),
        ]
        haystack_parts.extend(event.model_dump_json(by_alias=True) for event in sink.events)
        haystack_parts.extend(problem.detail for problem in catalog.snapshot.problems)
        haystack = "\n".join(haystack_parts)

        assert SENTINEL not in haystack


# ---------------------------------------------------------------------------
# T9: the HTTP client the factory built is closed on success and on exception
# ---------------------------------------------------------------------------


async def test_http_client_closes_after_success_and_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    recorded: list[dict[str, str]] = []

    async with _running_fake_app(recorded) as app:
        created: list[httpx2.AsyncClient] = []

        def builder(headers: dict[str, str]) -> httpx2.AsyncClient:
            client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)
            created.append(client)
            return client

        server = _auth_server()

        async with default_client_factory(server, http_client_builder=builder) as client:
            await client.list_tools()
        assert created[-1].is_closed

        class _Marker(Exception):
            pass

        # The MCP SDK's own session teardown runs inside a nested `anyio`
        # task group, so an exception raised in the caller's `async with`
        # body surfaces wrapped in a `BaseExceptionGroup` by the time it
        # reaches here -- that wrapping is the SDK's, not this wrapper's:
        # `_authenticated_http_client` neither catches nor reclassifies
        # anything. What matters is that the marker itself is neither
        # swallowed nor replaced by a generic transport error.
        with pytest.raises(BaseException) as excinfo:
            async with default_client_factory(server, http_client_builder=builder) as _client:
                raise _Marker("caller-body exception must propagate untouched")
        assert _contains_exception(excinfo.value, _Marker)
        assert created[-1].is_closed


# ---------------------------------------------------------------------------
# T10: a missing/blank variable degrades exactly one server, others still discovered
# ---------------------------------------------------------------------------


async def test_missing_env_var_yields_one_safe_problem_other_server_still_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AUTH_ENV_VAR, raising=False)
    recorded: list[dict[str, str]] = []

    async with _running_fake_app(recorded) as app:
        auth_factory = functools.partial(default_client_factory, http_client_builder=_asgi_http_client_builder(app))

        hvac_fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", lambda: {})])
        hvac_factory = in_memory_factory({"hvac": hvac_fake})

        def factory(server: ServerConfig) -> Any:
            if server.name == "hvac":
                return hvac_factory(server)
            return auth_factory(server)

        alice = _auth_server()
        hvac = ServerConfig(name="hvac", transport=StdioTransport(kind="stdio", command="hvac-cmd"))
        config = RuntimeConfig(
            servers=(alice, hvac),
            capabilities=(
                _capability(),
                CapabilityConfig(
                    server="hvac",
                    tool="set_target_temperature",
                    goal_prefixes=("other_thing",),
                    target=TargetMatch(type="environment"),
                ),
            ),
        )
        catalog = CapabilityCatalog(config, client_factory=factory)
        snapshot = await catalog.refresh()

        server_unavailable = [p for p in snapshot.problems if p.kind == "server_unavailable"]
        assert len(server_unavailable) == 1
        problem = server_unavailable[0]
        assert problem.server == "alice"
        assert AUTH_ENV_VAR in problem.detail
        assert "alice" in problem.detail
        assert SENTINEL not in problem.detail

        assert any(
            entry.server.name == "hvac" and entry.tool.name == "set_target_temperature" for entry in snapshot.entries
        )


# ---------------------------------------------------------------------------
# T11: a transport failure for an authenticated server is a safe, class-only diagnostic
# ---------------------------------------------------------------------------


async def test_transport_failure_for_authenticated_server_is_class_only_and_leak_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)

    class ExplodingAuthError(Exception):
        pass

    def failing_builder(headers: dict[str, str]) -> httpx2.AsyncClient:
        # A hostile/buggy SDK exception that embeds the full header and sentinel.
        raise ExplodingAuthError(f"connect failed; would-be headers were {headers}")

    server = _auth_server()
    config = RuntimeConfig(servers=(server,), capabilities=())
    factory = functools.partial(default_client_factory, http_client_builder=failing_builder)
    catalog = CapabilityCatalog(config, client_factory=factory)

    snapshot = await catalog.refresh()

    assert len(snapshot.problems) == 1
    problem = snapshot.problems[0]
    assert problem.kind == "server_unavailable"
    assert SENTINEL not in problem.detail
    assert "Authorization" not in problem.detail
    assert "ExplodingAuthError" in problem.detail
