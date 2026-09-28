from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from newton_mcp.runtime.config import (
    RUNTIME_CONFIG_ENV_VAR,
    HttpTransport,
    RuntimeConfig,
    ServerConfig,
    StdioTransport,
    load_runtime_config,
)

MINIMAL_CONFIG: dict = {
    "servers": [
        {"name": "hvac", "transport": {"kind": "stdio", "command": "hvac-server"}},
    ],
    "capabilities": [
        {
            "server": "hvac",
            "tool": "set_target_temperature",
            "goal_prefixes": ["reduce_room_temperature"],
            "target": {"type": "environment"},
        }
    ],
}


def test_minimal_config_loads_and_identity_defaults_to_name() -> None:
    config = RuntimeConfig.model_validate(MINIMAL_CONFIG)
    assert config.servers[0].name == "hvac"
    assert config.servers[0].identity is None
    assert config.servers[0].resolved_identity == "hvac"


def test_explicit_identity_is_kept() -> None:
    data = {
        "servers": [
            {
                "name": "hvac",
                "identity": "hvac-v2",
                "transport": {"kind": "stdio", "command": "hvac-server"},
            }
        ],
        "capabilities": [],
    }
    config = RuntimeConfig.model_validate(data)
    assert config.servers[0].resolved_identity == "hvac-v2"


@pytest.mark.parametrize(
    "mutate_path",
    [
        ("servers", 0, "unknown_key"),
        ("servers", 0, "transport", "unknown_key"),
        ("capabilities", 0, "unknown_key"),
        ("capabilities", 0, "target", "unknown_key"),
    ],
)
def test_unknown_key_is_rejected(mutate_path: tuple) -> None:
    import copy

    data = copy.deepcopy(MINIMAL_CONFIG)
    # Walk to the container named by every step but the last, and inject an
    # unknown key there under the last step's name.
    container = data
    for step in mutate_path[:-1]:
        container = container[step]
    container[mutate_path[-1]] = "surprise"

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)


def test_duplicate_server_names_rejected() -> None:
    data = {
        "servers": [
            {"name": "hvac", "transport": {"kind": "stdio", "command": "a"}},
            {"name": "hvac", "transport": {"kind": "stdio", "command": "b"}},
        ],
        "capabilities": [],
    }
    with pytest.raises(ValidationError, match="duplicate server name"):
        RuntimeConfig.model_validate(data)


def test_duplicate_resolved_identity_rejected() -> None:
    data = {
        "servers": [
            {"name": "hvac-a", "identity": "shared", "transport": {"kind": "stdio", "command": "a"}},
            {"name": "hvac-b", "identity": "shared", "transport": {"kind": "stdio", "command": "b"}},
        ],
        "capabilities": [],
    }
    with pytest.raises(ValidationError, match="duplicate resolved server identity"):
        RuntimeConfig.model_validate(data)


def test_capability_referencing_undeclared_server_rejected() -> None:
    data = {
        "servers": [{"name": "hvac", "transport": {"kind": "stdio", "command": "a"}}],
        "capabilities": [
            {
                "server": "ghost",
                "tool": "set_target_temperature",
                "goal_prefixes": ["reduce_room_temperature"],
                "target": {"type": "environment"},
            }
        ],
    }
    with pytest.raises(ValidationError, match="undeclared server"):
        RuntimeConfig.model_validate(data)


def test_duplicate_server_tool_pair_rejected() -> None:
    data = {
        "servers": [{"name": "hvac", "transport": {"kind": "stdio", "command": "a"}}],
        "capabilities": [
            {
                "server": "hvac",
                "tool": "set_target_temperature",
                "goal_prefixes": ["reduce_room_temperature"],
                "target": {"type": "environment"},
            },
            {
                "server": "hvac",
                "tool": "set_target_temperature",
                "goal_prefixes": ["raise_room_temperature"],
                "target": {"type": "environment"},
            },
        ],
    }
    with pytest.raises(ValidationError, match="duplicate capability"):
        RuntimeConfig.model_validate(data)


@pytest.mark.parametrize("blank_prefix", ["", "   ", "\t"])
def test_blank_goal_prefix_rejected(blank_prefix: str) -> None:
    data = {
        "servers": [{"name": "hvac", "transport": {"kind": "stdio", "command": "a"}}],
        "capabilities": [
            {
                "server": "hvac",
                "tool": "set_target_temperature",
                "goal_prefixes": ["reduce_room_temperature", blank_prefix],
                "target": {"type": "environment"},
            }
        ],
    }
    with pytest.raises(ValidationError, match="goal_prefixes"):
        RuntimeConfig.model_validate(data)


def test_load_runtime_config_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RUNTIME_CONFIG_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match=RUNTIME_CONFIG_ENV_VAR):
        load_runtime_config()


def test_load_runtime_config_env_var_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RUNTIME_CONFIG_ENV_VAR, "   ")
    with pytest.raises(ValueError, match=RUNTIME_CONFIG_ENV_VAR):
        load_runtime_config()


def test_load_runtime_config_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(ValueError, match=str(missing)):
        load_runtime_config(missing)


def test_load_runtime_config_malformed_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "runtime.yaml"
    bad.write_text("servers: [this is: not: valid: yaml")
    with pytest.raises(ValueError, match=str(bad)):
        load_runtime_config(bad)


def test_load_runtime_config_reads_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_CONFIG))
    config = load_runtime_config(path)
    assert isinstance(config, RuntimeConfig)
    assert config.servers[0].name == "hvac"


def test_load_runtime_config_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_CONFIG))
    monkeypatch.setenv(RUNTIME_CONFIG_ENV_VAR, str(path))
    config = load_runtime_config()
    assert config.servers[0].name == "hvac"


_FORBIDDEN_SUBSTRINGS = ("lock", "oven", "alarm", "industrial", "safety", "start", "stop")


def _http_server(url: str, name: str = "home-bridge") -> ServerConfig:
    return ServerConfig(name=name, transport=HttpTransport(kind="streamable-http", url=url))


def _stdio_server(
    command: str = "hvac-server",
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    name: str = "hvac",
) -> ServerConfig:
    return ServerConfig(
        name=name, transport=StdioTransport(kind="stdio", command=command, args=args, env=env or {})
    )


# ---------------------------------------------------------------------------
# T5: binding_identity starts with resolved_identity and differs from it
# ---------------------------------------------------------------------------


def test_binding_identity_starts_with_resolved_identity_and_differs() -> None:
    server = _http_server("https://home-bridge.local/mcp")
    assert server.binding_identity.startswith(server.resolved_identity)
    assert server.binding_identity != server.resolved_identity


# ---------------------------------------------------------------------------
# T6: changing url / command / args changes transport_fingerprint
# ---------------------------------------------------------------------------


def test_changing_http_url_changes_fingerprint() -> None:
    a = _http_server("https://home-bridge.local/mcp")
    b = _http_server("https://other-host.local/mcp")
    assert a.transport_fingerprint != b.transport_fingerprint


def test_changing_stdio_command_changes_fingerprint() -> None:
    a = _stdio_server(command="hvac-server")
    b = _stdio_server(command="other-server")
    assert a.transport_fingerprint != b.transport_fingerprint


def test_changing_stdio_args_changes_fingerprint() -> None:
    a = _stdio_server(args=("--config", "a.yaml"))
    b = _stdio_server(args=("--config", "b.yaml"))
    assert a.transport_fingerprint != b.transport_fingerprint


# ---------------------------------------------------------------------------
# T7: _canonical_url via transport_fingerprint
# ---------------------------------------------------------------------------


def test_canonical_url_ignores_scheme_and_host_case_and_default_port() -> None:
    a = _http_server("https://Host.local/mcp")
    b = _http_server("https://host.local:443/mcp")
    c = _http_server("https://host.local/mcp")
    assert a.transport_fingerprint == b.transport_fingerprint == c.transport_fingerprint


def test_canonical_url_trailing_slash_is_distinct() -> None:
    a = _http_server("https://host.local/mcp/")
    b = _http_server("https://host.local/mcp")
    assert a.transport_fingerprint != b.transport_fingerprint


def test_canonical_url_userinfo_is_kept_and_distinguishes() -> None:
    alice = _http_server("https://alice:x@host.local/mcp")
    bob = _http_server("https://bob:x@host.local/mcp")
    bare = _http_server("https://host.local/mcp")
    assert alice.transport_fingerprint != bob.transport_fingerprint
    assert alice.transport_fingerprint != bare.transport_fingerprint


def test_canonical_url_ipv6_keeps_brackets_and_non_default_port() -> None:
    with_port = _http_server("http://[::1]:8080/mcp")
    default_port = _http_server("http://[::1]:80/mcp")
    no_port = _http_server("http://[::1]/mcp")
    assert default_port.transport_fingerprint == no_port.transport_fingerprint
    assert with_port.transport_fingerprint != no_port.transport_fingerprint


# ---------------------------------------------------------------------------
# T8: env values/names in the stdio fingerprint (owner amendment)
# ---------------------------------------------------------------------------


def test_changing_env_value_changes_fingerprint() -> None:
    a = _stdio_server(env={"HA_URL": "http://old.local"})
    b = _stdio_server(env={"HA_URL": "http://new.local"})
    assert a.transport_fingerprint != b.transport_fingerprint


def test_adding_removing_renaming_env_var_changes_fingerprint() -> None:
    base = _stdio_server(env={"HA_URL": "http://x.local"})
    added = _stdio_server(env={"HA_URL": "http://x.local", "HA_TOKEN": "secret"})
    removed = _stdio_server(env={})
    renamed = _stdio_server(env={"HA_URL_2": "http://x.local"})
    assert base.transport_fingerprint != added.transport_fingerprint
    assert base.transport_fingerprint != removed.transport_fingerprint
    assert base.transport_fingerprint != renamed.transport_fingerprint


def test_env_insertion_order_does_not_affect_fingerprint() -> None:
    a = _stdio_server(env={"A": "1", "B": "2"})
    b = _stdio_server(env={"B": "2", "A": "1"})
    assert a.transport_fingerprint == b.transport_fingerprint


def test_binding_identity_never_contains_env_value() -> None:
    server = _stdio_server(env={"HA_URL": "http://super-secret-host.local", "HA_TOKEN": "abc123"})
    assert "super-secret-host" not in server.binding_identity
    assert "abc123" not in server.binding_identity


def test_example_runtime_config_is_valid_and_safe() -> None:
    example_path = Path(__file__).resolve().parents[2] / "examples" / "runtime.example.yaml"
    config = load_runtime_config(example_path)
    assert config.servers
    assert config.capabilities
    for capability in config.capabilities:
        haystack = " ".join([capability.tool, *capability.goal_prefixes]).lower()
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in haystack, (
                f"capability {capability.server}/{capability.tool} looks unsafe: "
                f"matched forbidden term {forbidden!r}"
            )
