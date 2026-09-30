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
import logging
import traceback

import anyio
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
from newton_mcp.runtime.auth import AuthTransportError, MissingAuthSecret
from newton_mcp.runtime.executor import ExecutorError
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
        # Reuse the approval created before rotation, resolving the new value only at connect.
        monkeypatch.setenv(AUTH_ENV_VAR, "rotated-token")

        record, outcome = await executor.execute(
            candidate, record, approval=approval, policy_version="policy.v1", now=NOW
        )
        assert outcome.state == ActionState.EXECUTED
        assert recorded, "call_tool should have produced at least one recorded request"
        assert all(h.get("authorization") == "Bearer rotated-token" for h in recorded)


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
        assert recorded
        assert all(h.get("authorization") == f"Bearer {SENTINEL}" for h in recorded)

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

        # Caller-body errors retain their identity; SDK teardown cannot
        # replace them with a transport error.
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


@pytest.mark.parametrize("stage", ["builder", "http_enter", "connect", "list", "call", "sdk_close", "http_close"])
@pytest.mark.parametrize("grouped", [False, True])
async def test_authenticated_boundary_contains_failures_and_logs(
    monkeypatch, caplog, stage, grouped,
) -> None:
    import newton_mcp.runtime.catalog as module

    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    caplog.set_level(logging.DEBUG)
    closed = []

    def fail(at):
        if stage != at:
            return
        # Rotate after the header was captured: redaction must not re-read env.
        monkeypatch.setenv(AUTH_ENV_VAR, "rotated-token")
        error = RuntimeError(f"reflected Bearer {SENTINEL}")
        if grouped:
            error = ExceptionGroup(f"group {SENTINEL}", [ExceptionGroup("nested", [error])])
        try:
            raise error
        except Exception:
            logging.getLogger("mcp.test_transport").exception("reflected %s", SENTINEL)
            raise

    class HttpClient:
        async def __aenter__(self):
            fail("http_enter")
            return self

        async def __aexit__(self, *args):
            closed.append("http")
            fail("http_close")

    class SdkClient:
        server_info = None

        async def __aenter__(self):
            fail("connect")
            return self

        async def __aexit__(self, *args):
            closed.append("sdk")
            fail("sdk_close")

        async def list_tools(self, **kwargs):
            fail("list")

        async def call_tool(self, *args):
            fail("call")

    def builder(headers):
        assert headers["Authorization"] == f"Bearer {SENTINEL}"
        fail("builder")
        return HttpClient()

    monkeypatch.setattr(module, "Client", lambda transport: SdkClient())
    with pytest.raises((AuthTransportError, ExceptionGroup)) as caught:
        async with default_client_factory(_auth_server(), http_client_builder=builder) as client:
            await client.list_tools()
            await client.call_tool("ping", {})
    assert "RuntimeError" in str(caught.value)
    for rendered in (str(caught.value), repr(caught.value), "".join(traceback.format_exception(caught.value)), caplog.text):
        assert SENTINEL not in rendered
    if stage not in ("builder", "http_enter"):
        assert "http" in closed
    if stage in ("list", "call", "sdk_close", "http_close"):
        assert closed == ["sdk", "http"]


@pytest.mark.parametrize("value", ["tokené", "token\tbits", "token\n"])
async def test_invalid_secret_never_reaches_builder(monkeypatch, value) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, value)
    calls = []
    def builder(headers):
        calls.append(headers)
        raise AssertionError("must not construct a client")
    with pytest.raises(MissingAuthSecret):
        async with default_client_factory(_auth_server(), http_client_builder=builder):
            pass
    assert calls == []


@pytest.mark.parametrize("field,new_value", [("env", "OTHER_TOKEN"), ("header", "X-Api-Key"), ("scheme", "Token")])
async def test_auth_identity_change_rejects_old_candidate_before_connect(monkeypatch, field, new_value) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    server = _auth_server()
    candidate = _candidate(server)
    record = _authorized_record()
    approval = _approval(candidate, record)
    auth = server.transport.auth.model_copy(update={field: new_value})
    changed = server.model_copy(update={"transport": server.transport.model_copy(update={"auth": auth})})
    calls = []
    def factory(server):
        calls.append(server)
        raise AssertionError("must reject before connecting")
    executor = Executor(CapabilityCatalog(RuntimeConfig(servers=(changed,))), client_factory=factory)
    with pytest.raises(ExecutorError):
        await executor.execute(candidate, record, approval=approval, policy_version="policy.v1", now=NOW)
    assert calls == []


async def test_authenticated_client_closes_on_cancellation(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    recorded = []
    created = []
    async with _running_fake_app(recorded) as app:
        def builder(headers):
            client = _asgi_http_client_builder(app)(headers)
            created.append(client)
            return client
        with anyio.CancelScope() as scope:
            async with default_client_factory(_auth_server(), http_client_builder=builder):
                scope.cancel()
                await anyio.sleep(0)
        assert scope.cancelled_caught
        assert created[-1].is_closed


async def test_authenticated_transport_does_not_forward_redirect(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx2.Response(307, headers={"location": "https://other.example/mcp"})
    created = []
    def builder(headers):
        client = httpx2.AsyncClient(headers=headers, transport=httpx2.MockTransport(respond), follow_redirects=True)
        created.append(client)
        return client
    with pytest.raises(AuthTransportError):
        async with default_client_factory(_auth_server(), http_client_builder=builder):
            pass
    assert requests
    assert all(request.url.host == "localhost" for request in requests)
    assert created[-1].is_closed


async def test_real_sdk_connection_failure_is_safe_in_exception_and_logs(monkeypatch, caplog) -> None:
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    caplog.set_level(logging.DEBUG)
    created = []
    def respond(request):
        raise RuntimeError(f"request headers: {request.headers!r}")
    def builder(headers):
        client = httpx2.AsyncClient(headers=headers, transport=httpx2.MockTransport(respond))
        created.append(client)
        return client
    with pytest.raises(Exception) as caught:
        async with default_client_factory(_auth_server(), http_client_builder=builder):
            pass
    for rendered in (str(caught.value), repr(caught.value), "".join(traceback.format_exception(caught.value)), caplog.text):
        assert SENTINEL not in rendered
    assert created[-1].is_closed


async def test_caller_exception_and_logs_keep_identity_even_when_close_fails(monkeypatch, caplog) -> None:
    import newton_mcp.runtime.catalog as module
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    caplog.set_level(logging.INFO)
    marker = ValueError("caller marker")
    class Context:
        server_info = None
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            raise RuntimeError(SENTINEL)
    monkeypatch.setattr(module, "Client", lambda transport: Context())
    with pytest.raises(ValueError) as caught:
        async with default_client_factory(_auth_server(), http_client_builder=lambda headers: Context()):
            logging.getLogger("caller").info("caller diagnostic stays unchanged")
            raise marker
    assert caught.value is marker
    assert "caller diagnostic stays unchanged" in caplog.text
    assert SENTINEL not in "".join(traceback.format_exception(caught.value))


async def test_sdk_mixed_exception_group_preserves_cancellation_without_secret(monkeypatch) -> None:
    import asyncio
    import newton_mcp.runtime.catalog as module
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    cancelled = asyncio.CancelledError()
    class Context:
        server_info = None
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def list_tools(self, **kwargs):
            raise BaseExceptionGroup(SENTINEL, [cancelled, RuntimeError(SENTINEL)])
    monkeypatch.setattr(module, "Client", lambda transport: Context())
    with pytest.raises(BaseExceptionGroup) as caught:
        async with default_client_factory(_auth_server(), http_client_builder=lambda headers: Context()) as client:
            await client.list_tools()
    assert caught.value.exceptions[0] is cancelled
    assert isinstance(caught.value.exceptions[1], AuthTransportError)
    assert SENTINEL not in "".join(traceback.format_exception(caught.value))


async def test_connect_cancellation_is_not_replaced_by_close_failure(monkeypatch) -> None:
    import asyncio
    import newton_mcp.runtime.catalog as module
    monkeypatch.setenv(AUTH_ENV_VAR, SENTINEL)
    cancelled = asyncio.CancelledError()
    closed = []
    class HttpContext:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            closed.append(True)
            raise RuntimeError(SENTINEL)
    class SdkContext:
        async def __aenter__(self):
            raise cancelled
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(module, "Client", lambda transport: SdkContext())
    with pytest.raises(asyncio.CancelledError) as caught:
        async with default_client_factory(_auth_server(), http_client_builder=lambda headers: HttpContext()):
            pass
    assert caught.value is cancelled
    assert closed == [True]
