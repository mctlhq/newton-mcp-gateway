"""In-process fake MCP actuator for the smart-home testbed (`examples/smart-home/`).

"Alice" here is a stand-in for an open-source smart-home MCP server: a plausible
tool shape (`get_room_state`, `set_ac_temperature`, `set_light_state`,
`set_light_brightness`, `announce`) chosen because the real server's exact
tools are not drivable by this runtime yet (see `examples/smart-home/README.md`).
Everything in this module is actuator-specific and deliberately lives under
`examples/`, never under `src/newton_mcp/` -- the runtime itself stays
actuator-agnostic.

Built with `mcp.server.mcpserver.MCPServer`, exactly like
`tests/runtime/conftest.py::build_fake_server`: no subprocess, no socket.
`in_process_factory()` returns a `ClientFactory` (the same seam
`CapabilityCatalog`, `Executor` and `Verifier` all accept) that connects to
the fake server in-process via `mcp.Client(MCPServer)`, ignoring whatever
transport `runtime.yaml` declares.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from newton_mcp.runtime.catalog import ClientFactory, SupportsListTools
from newton_mcp.runtime.config import ServerConfig


class RoomState(BaseModel):
    """The simulated kitchen `fake_alice`'s tools read and write.

    Not frozen -- unlike most models in `newton_mcp` -- because the whole
    point of this fixture is a small piece of mutable state the registered
    tool handlers update between calls. `cooling_step_c` and `announcements`
    are testbed-only bookkeeping, not part of any real Alice API.
    """

    model_config = ConfigDict(extra="forbid")

    room: str
    temperature_c: float
    occupancy: bool = True
    ac_online: bool = True
    ac_target_c: int | None = None
    light_on: bool = False
    brightness_pct: int = 0
    cooling_step_c: float = 2.0
    announcements: list[str] = Field(default_factory=list)


def _advance_toward_target(state: RoomState) -> None:
    """Move `state.temperature_c` one `cooling_step_c` toward `ac_target_c`.

    Only while `ac_online` is true and a target has been set -- this is the
    one place the simulated settling delay lives, and it is what the
    "AC offline" failure mode disables: `set_ac_temperature` still records a
    target and still answers `{"accepted": true}` even while the AC is
    offline, but the temperature never moves.
    """
    if not state.ac_online or state.ac_target_c is None:
        return
    diff = state.ac_target_c - state.temperature_c
    if abs(diff) <= state.cooling_step_c:
        state.temperature_c = float(state.ac_target_c)
    elif diff > 0:
        state.temperature_c += state.cooling_step_c
    else:
        state.temperature_c -= state.cooling_step_c


def build_fake_alice(
    state: RoomState,
    *,
    call_log: list[tuple[str, dict[str, Any]]] | None = None,
) -> MCPServer:
    """Build an in-process fake MCP actuator over `state`.

    Registers exactly five tools: the read-only `get_room_state` (annotated
    `read_only_hint=True`), three idempotent absolute setters
    (`set_ac_temperature`, `set_light_state`, `set_light_brightness`), and
    the non-idempotent `announce`, which deliberately has no read
    counterpart. Every call is appended to `call_log` as `(tool_name,
    arguments)`, so a caller can assert the exact number and shape of
    actuator calls a run issued. If `call_log` is omitted a private list is
    used internally (calls still happen; the caller just cannot observe them).
    """
    log: list[tuple[str, dict[str, Any]]] = call_log if call_log is not None else []

    server = MCPServer(name="alice", version="0.0.0-fake")

    async def get_room_state(room: str) -> dict[str, Any]:
        """Read the simulated room's current state (read-only)."""
        log.append(("get_room_state", {"room": room}))
        _advance_toward_target(state)
        return {
            "room": state.room,
            "temperature_c": state.temperature_c,
            "occupancy": state.occupancy,
            "ac_online": state.ac_online,
            "ac_target_c": state.ac_target_c,
            "light_on": state.light_on,
            "brightness_pct": state.brightness_pct,
        }

    server.add_tool(
        get_room_state,
        name="get_room_state",
        description="Read the simulated room's current state.",
        annotations=ToolAnnotations(read_only_hint=True),
    )

    async def set_ac_temperature(room: str, target_temperature_c: int) -> dict[str, Any]:
        """Set the AC's target temperature.

        Accepts any integer -- the 20-25 C safety band is enforced by
        `policy.yaml` alone, never by this schema. Records the target and
        answers `{"accepted": true}` whether or not the AC is online: a
        digitally successful call with no guaranteed physical effect, which
        is the exact failure `--ac-offline` demonstrates.
        """
        log.append(("set_ac_temperature", {"room": room, "target_temperature_c": target_temperature_c}))
        state.ac_target_c = target_temperature_c
        return {"accepted": True, "room": room, "target_temperature_c": target_temperature_c}

    server.add_tool(
        set_ac_temperature,
        name="set_ac_temperature",
        description="Set the AC's target temperature for a room. Idempotent absolute set.",
    )

    async def set_light_state(room: str, on: bool) -> dict[str, Any]:
        """Turn the room's light on or off. Idempotent absolute set."""
        log.append(("set_light_state", {"room": room, "on": on}))
        state.light_on = on
        return {"accepted": True, "room": room, "on": on}

    server.add_tool(
        set_light_state,
        name="set_light_state",
        description="Turn a room's light on or off. Idempotent absolute set.",
    )

    async def set_light_brightness(room: str, brightness_pct: int) -> dict[str, Any]:
        """Set the room light's brightness percentage. Idempotent absolute set."""
        log.append(("set_light_brightness", {"room": room, "brightness_pct": brightness_pct}))
        state.brightness_pct = brightness_pct
        return {"accepted": True, "room": room, "brightness_pct": brightness_pct}

    server.add_tool(
        set_light_brightness,
        name="set_light_brightness",
        description="Set a room light's brightness percentage. Idempotent absolute set.",
    )

    async def announce(message: str) -> dict[str, Any]:
        """Play a one-shot speaker announcement.

        Deliberately NOT idempotent, and has no read counterpart -- this is
        the capability that exercises the runtime's "no read_tool -> always
        ESCALATED, never retried" path.
        """
        log.append(("announce", {"message": message}))
        state.announcements.append(message)
        return {"accepted": True, "message": message}

    server.add_tool(
        announce,
        name="announce",
        description="Play a one-shot speaker announcement. Not idempotent; has no read counterpart.",
    )

    return server


def in_process_factory(server: MCPServer) -> ClientFactory:
    """A `ClientFactory` that connects in-process to `server` for any `ServerConfig`.

    Ignores the `ServerConfig`'s declared transport entirely -- the same seam
    `CapabilityCatalog`, `Executor` and `Verifier` all accept, which is why
    driving this fixture needs no subprocess and no socket.
    `ServerConfig.binding_identity` (and so the approval binding) is still
    computed from the *declared* transport in `runtime.yaml`, not from how
    this factory actually connects.
    """

    def factory(_server: ServerConfig) -> AbstractAsyncContextManager[SupportsListTools]:
        return Client(server)  # type: ignore[return-value]

    return factory
