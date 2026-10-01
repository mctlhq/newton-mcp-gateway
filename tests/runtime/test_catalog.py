from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.types import ToolAnnotations
from mcp_types import ListToolsResult, Tool

from newton_mcp.runtime.catalog import CapabilityCatalog, CatalogSnapshot, default_client_factory
from newton_mcp.runtime.config import (
    CapabilityConfig,
    HttpTransport,
    RuntimeConfig,
    ServerConfig,
    StdioTransport,
    TargetMatch,
)

from .conftest import (
    FakeToolSpec,
    build_fake_server,
    hanging_after_first_page_factory,
    hanging_factory,
    in_memory_factory,
    paginated_factory,
    raising_factory,
    task_group_raising_factory,
)

CALL_FLAGS: dict[str, bool] = {}


def _flagging_handler(flag_name: str):
    async def handler() -> dict:
        CALL_FLAGS[flag_name] = True
        return {}

    handler.__name__ = flag_name
    return handler


def _server_config(name: str) -> ServerConfig:
    return ServerConfig(name=name, transport=StdioTransport(kind="stdio", command=f"{name}-cmd"))


def _capability(server: str, tool: str, *, read_tool: str | None = None) -> CapabilityConfig:
    return CapabilityConfig(
        server=server,
        tool=tool,
        goal_prefixes=("do_thing",),
        target=TargetMatch(type="environment"),
        read_tool=read_tool,
    )


async def test_refresh_keeps_only_allow_listed_tools() -> None:
    CALL_FLAGS.clear()
    fake = build_fake_server(
        "hvac",
        [
            FakeToolSpec("set_target_temperature", _flagging_handler("set_target_temperature")),
            FakeToolSpec("get_room_temperature", _flagging_handler("get_room_temperature")),
            FakeToolSpec("reboot_gateway", _flagging_handler("reboot_gateway")),
        ],
    )
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature", read_tool="get_room_temperature"),),
    )
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))

    snapshot = await catalog.refresh()

    tool_names = {entry.tool.name for entry in snapshot.entries}
    assert tool_names == {"set_target_temperature"}
    assert not any(p.tool == "reboot_gateway" for p in snapshot.problems)
    assert not snapshot.problems


async def test_missing_allow_listed_tool_is_reported_and_catalog_stays_usable() -> None:
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", _flagging_handler("x"))])
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(
            _capability("hvac", "set_target_temperature"),
            _capability("hvac", "get_room_temperature"),
        ),
    )
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))

    snapshot = await catalog.refresh()

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].tool.name == "set_target_temperature"
    tool_missing = [p for p in snapshot.problems if p.kind == "tool_missing"]
    assert len(tool_missing) == 1
    assert tool_missing[0].server == "hvac"
    assert tool_missing[0].tool == "get_room_temperature"


async def test_missing_read_tool_is_reported_and_entry_still_resolves() -> None:
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", _flagging_handler("x"))])
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature", read_tool="get_room_temperature"),),
    )
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))

    snapshot = await catalog.refresh()

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].read_tool is None
    read_tool_missing = [p for p in snapshot.problems if p.kind == "read_tool_missing"]
    assert len(read_tool_missing) == 1
    assert read_tool_missing[0].tool == "get_room_temperature"


async def test_unreachable_server_reports_problem_others_still_discovered() -> None:
    healthy = build_fake_server("lighting", [FakeToolSpec("set_light_state", _flagging_handler("x"))])
    config = RuntimeConfig(
        servers=(_server_config("hvac"), _server_config("lighting")),
        capabilities=(
            _capability("hvac", "set_target_temperature"),
            _capability("lighting", "set_light_state"),
        ),
    )
    catalog = CapabilityCatalog(
        config, client_factory=in_memory_factory({"lighting": healthy}), server_timeout_seconds=1.0
    )
    # "hvac" is absent from the factory's map, so it raises a plain KeyError.

    snapshot = await catalog.refresh()

    unavailable = [p for p in snapshot.problems if p.kind == "server_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].server == "hvac"
    assert {e.server.name for e in snapshot.entries} == {"lighting"}


async def test_servers_are_discovered_concurrently_and_snapshot_keeps_configured_order() -> None:
    entered: set[str] = set()
    completed: list[str] = []
    both_entered = anyio.Event()
    second_completed = anyio.Event()
    release_first = anyio.Event()

    @asynccontextmanager
    async def factory(server: ServerConfig):
        class Session:
            server_info = None

            async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
                entered.add(server.name)
                if len(entered) == 2:
                    both_entered.set()
                if server.name == "first":
                    await release_first.wait()
                return ListToolsResult(tools=[Tool(name=f"{server.name}_tool", input_schema={"type": "object"})])

        yield Session()

    config = RuntimeConfig(
        servers=(_server_config("first"), _server_config("second")),
        capabilities=(_capability("first", "first_tool"), _capability("second", "second_tool")),
    )
    catalog = CapabilityCatalog(config, client_factory=factory)
    discover_server = catalog._discover_server

    async def track_completion(server: ServerConfig):
        result = await discover_server(server)
        completed.append(server.name)
        if server.name == "second":
            second_completed.set()
        return result

    catalog._discover_server = track_completion  # type: ignore[method-assign]

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(catalog.refresh)
        with anyio.fail_after(2):
            await both_entered.wait()
            await second_completed.wait()
        release_first.set()

    assert entered == {"first", "second"}
    assert completed == ["second", "first"]
    assert [entry.server.name for entry in catalog.snapshot.entries] == ["first", "second"]


async def test_exception_group_from_connect_is_reported_as_server_unavailable() -> None:
    config = RuntimeConfig(
        servers=(_server_config("flaky"),),
        capabilities=(_capability("flaky", "some_tool"),),
    )
    catalog = CapabilityCatalog(
        config, client_factory=task_group_raising_factory("boom"), server_timeout_seconds=1.0
    )

    snapshot = await catalog.refresh()

    assert len(snapshot.problems) == 1
    assert snapshot.problems[0].kind == "server_unavailable"
    assert snapshot.problems[0].server == "flaky"
    # refresh() must not raise even though the underlying failure was an ExceptionGroup.


async def test_synchronous_factory_error_is_reported_as_server_unavailable() -> None:
    config = RuntimeConfig(
        servers=(_server_config("flaky"),),
        capabilities=(_capability("flaky", "some_tool"),),
    )
    catalog = CapabilityCatalog(config, client_factory=raising_factory(RuntimeError("nope")), server_timeout_seconds=1.0)

    snapshot = await catalog.refresh()

    assert len(snapshot.problems) == 1
    assert snapshot.problems[0].kind == "server_unavailable"
    assert "nope" in snapshot.problems[0].detail


async def test_cancellation_propagates_and_previous_snapshot_is_kept() -> None:
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature"),),
    )
    catalog = CapabilityCatalog(config, client_factory=hanging_factory(), server_timeout_seconds=100.0)
    previous_snapshot = catalog.snapshot

    with anyio.move_on_after(0.05) as scope:
        await catalog.refresh()

    assert scope.cancelled_caught
    assert catalog.snapshot == previous_snapshot
    assert catalog.snapshot.problems == ()


async def test_hung_server_times_out_without_blocking_other_servers() -> None:
    healthy = build_fake_server("lighting", [FakeToolSpec("set_light_state", _flagging_handler("x"))])
    config = RuntimeConfig(
        servers=(_server_config("hvac"), _server_config("lighting")),
        capabilities=(
            _capability("hvac", "set_target_temperature"),
            _capability("lighting", "set_light_state"),
        ),
    )

    def factory(server: ServerConfig):
        if server.name == "hvac":
            return hanging_factory()(server)
        return in_memory_factory({"lighting": healthy})(server)

    catalog = CapabilityCatalog(config, client_factory=factory, server_timeout_seconds=0.05)

    snapshot = await catalog.refresh()

    unavailable = [p for p in snapshot.problems if p.kind == "server_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].server == "hvac"
    assert "TimeoutError" in unavailable[0].detail
    assert {e.server.name for e in snapshot.entries} == {"lighting"}


async def test_hang_on_connect_is_also_bounded_by_the_timeout() -> None:
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature"),),
    )
    catalog = CapabilityCatalog(
        config, client_factory=hanging_factory(hang_on_connect=True), server_timeout_seconds=0.05
    )

    snapshot = await catalog.refresh()

    assert len(snapshot.problems) == 1
    assert snapshot.problems[0].kind == "server_unavailable"
    assert "TimeoutError" in snapshot.problems[0].detail


async def test_hang_on_second_page_is_also_bounded_by_the_timeout() -> None:
    first_page = [Tool(name="set_target_temperature", input_schema={"type": "object"})]
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature"),),
    )
    catalog = CapabilityCatalog(
        config,
        client_factory=hanging_after_first_page_factory(first_page),
        server_timeout_seconds=0.05,
    )

    snapshot = await catalog.refresh()

    assert len(snapshot.problems) == 1
    assert snapshot.problems[0].kind == "server_unavailable"
    assert not snapshot.entries


async def test_observed_server_info_is_metadata_only() -> None:
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", _flagging_handler("x"))])
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature"),),
    )
    # A fresh MCPServer instance per connect avoids the mcp SDK's per-instance
    # `serverInfo` stamp cache (`Server._server_info_stamp_source` is a
    # `cached_property`), so each `factory()` call below observes the name
    # that instance was actually built with.
    renamed_fake = build_fake_server(
        "renamed-hvac-server", [FakeToolSpec("set_target_temperature", _flagging_handler("x"))]
    )
    fakes = {"hvac": fake}
    catalog = CapabilityCatalog(config, client_factory=lambda server: in_memory_factory(fakes)(server))

    snapshot = await catalog.refresh()

    assert len(snapshot.server_info) == 1
    assert snapshot.server_info[0].server == "hvac"
    assert snapshot.server_info[0].name == "hvac"

    from newton_mcp.action.contract import PhysicalActionContract, Risk, Target, Verification
    from newton_mcp.runtime.resolver import Resolver

    contract = PhysicalActionContract(
        goal="do_thing_now",
        reason="test",
        target=Target(type="environment"),
        risk=Risk.LOW,
        verification=Verification(condition={"path": "state", "op": "eq", "value": "ok"}),
    )
    before = Resolver(catalog).resolve(contract)

    # Swap in a server that reports a different observed serverInfo.name; this
    # must not change any CandidateAction (server_identity stays the configured one).
    fakes["hvac"] = renamed_fake
    await catalog.refresh()
    after = Resolver(catalog).resolve(contract)

    assert catalog.snapshot.server_info[0].name == "renamed-hvac-server"
    assert before.candidates == after.candidates


async def test_pagination_reads_every_page_until_exhaustion() -> None:
    pages = [
        [Tool(name="set_target_temperature", input_schema={"type": "object"})],
        [Tool(name="get_room_temperature", input_schema={"type": "object"})],
    ]
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature", read_tool="get_room_temperature"),),
    )
    catalog = CapabilityCatalog(config, client_factory=paginated_factory(pages))

    snapshot = await catalog.refresh()

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].read_tool is not None
    assert snapshot.entries[0].read_tool.name == "get_room_temperature"


async def test_refresh_reflects_tools_added_and_removed_between_refreshes() -> None:
    fake = build_fake_server("hvac", [FakeToolSpec("set_target_temperature", _flagging_handler("x"))])
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature", read_tool="get_room_temperature"),),
    )
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))

    assert catalog.snapshot == CatalogSnapshot()

    first = await catalog.refresh()
    assert first.entries[0].read_tool is None  # get_room_temperature not added yet

    fake.add_tool(_flagging_handler("get_room_temperature"), name="get_room_temperature")
    second = await catalog.refresh()
    assert second.entries[0].read_tool is not None

    fake.remove_tool("get_room_temperature")
    third = await catalog.refresh()
    assert third.entries[0].read_tool is None


async def test_refresh_never_calls_call_tool() -> None:
    CALL_FLAGS.clear()
    fake = build_fake_server(
        "hvac",
        [
            FakeToolSpec("set_target_temperature", _flagging_handler("set_target_temperature")),
            FakeToolSpec("get_room_temperature", _flagging_handler("get_room_temperature")),
        ],
    )
    config = RuntimeConfig(
        servers=(_server_config("hvac"),),
        capabilities=(_capability("hvac", "set_target_temperature", read_tool="get_room_temperature"),),
    )
    catalog = CapabilityCatalog(config, client_factory=in_memory_factory({"hvac": fake}))

    await catalog.refresh()

    assert CALL_FLAGS.get("set_target_temperature") is not True
    assert CALL_FLAGS.get("get_room_temperature") is not True


# ---------------------------------------------------------------------------
# T12: default_client_factory's unchanged branches (issue-28 collateral-damage guard)
# ---------------------------------------------------------------------------


def test_default_client_factory_http_without_auth_returns_plain_client() -> None:
    server = ServerConfig(
        name="home-bridge", transport=HttpTransport(kind="streamable-http", url="https://home-bridge.local/mcp")
    )
    client = default_client_factory(server)
    assert isinstance(client, Client)
    assert client.server == "https://home-bridge.local/mcp"


def test_default_client_factory_stdio_still_builds_stdio_server_parameters() -> None:
    server = ServerConfig(
        name="hvac", transport=StdioTransport(kind="stdio", command="hvac-server", args=("--flag",), env={"A": "1"})
    )
    client = default_client_factory(server)
    assert isinstance(client, Client)
    assert isinstance(client.server, StdioServerParameters)
    assert client.server.command == "hvac-server"
    assert client.server.args == ["--flag"]
    assert client.server.env == {"A": "1"}


# ---------------------------------------------------------------------------
# T13: SDK-surface guard for `create_mcp_http_client`
# ---------------------------------------------------------------------------


async def test_create_mcp_http_client_surface_still_accepts_headers() -> None:
    """A future `mcp` bump that moves/changes this helper fails here, with an obvious cause."""
    from mcp.shared._httpx_utils import create_mcp_http_client

    client = create_mcp_http_client(headers={"X-Test": "y"})
    try:
        assert client.headers["X-Test"] == "y"
    finally:
        await client.aclose()
