from __future__ import annotations

import traceback
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from newton_mcp.runtime.config import (
    RUNTIME_CONFIG_ENV_VAR,
    AuthConfigError,
    HttpAuth,
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


# ---------------------------------------------------------------------------
# issue-28: `auth` block on a streamable-http transport
# ---------------------------------------------------------------------------


def _http_server_with_auth(
    url: str = "https://home-bridge.local/mcp",
    *,
    header: str = "Authorization",
    scheme: str | None = "Bearer",
    env: str = "ALICE_MCP_TOKEN",
    name: str = "home-bridge",
) -> ServerConfig:
    return ServerConfig(
        name=name,
        transport=HttpTransport(kind="streamable-http", url=url, auth={"header": header, "scheme": scheme, "env": env}),
    )


# T1: a valid auth block loads; an omitted auth leaves it None.


def test_valid_auth_block_loads() -> None:
    server = _http_server_with_auth()
    transport = server.transport
    assert isinstance(transport, HttpTransport)
    assert transport.auth == HttpAuth(header="Authorization", scheme="Bearer", env="ALICE_MCP_TOKEN")


def test_omitted_auth_is_none() -> None:
    server = _http_server("https://home-bridge.local/mcp")
    transport = server.transport
    assert isinstance(transport, HttpTransport)
    assert transport.auth is None


def test_explicit_auth_null_is_treated_as_omitted() -> None:
    config = RuntimeConfig.model_validate(
        {
            "servers": [
                {
                    "name": "home-bridge",
                    "transport": {"kind": "streamable-http", "url": "https://home-bridge.local/mcp", "auth": None},
                }
            ],
            "capabilities": [],
        }
    )
    transport = config.servers[0].transport
    assert isinstance(transport, HttpTransport)
    assert transport.auth is None


# T2: the leak test. An inline literal secret raises `AuthConfigError`, and
# the sentinel never appears in `str`/`repr`/a formatted traceback.

_SENTINEL = "sk-live-super-secret-DO-NOT-LEAK-0123456789"  # noqa: S105 - test sentinel, not a real credential


@pytest.mark.parametrize("secret_key", ["value", "token", "secret", "password"])
def test_inline_secret_in_auth_block_is_rejected_without_leaking(secret_key: str) -> None:
    data = {
        "servers": [
            {
                "name": "home-bridge",
                "transport": {
                    "kind": "streamable-http",
                    "url": "https://home-bridge.local/mcp",
                    "auth": {"header": "Authorization", secret_key: _SENTINEL},
                },
            }
        ],
        "capabilities": [],
    }
    with pytest.raises(AuthConfigError) as excinfo:
        RuntimeConfig.model_validate(data)

    exc = excinfo.value
    assert _SENTINEL not in str(exc)
    assert _SENTINEL not in repr(exc)
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _SENTINEL not in formatted


def test_auth_config_error_is_not_a_value_error() -> None:
    assert not issubclass(AuthConfigError, ValueError)
    assert not issubclass(AuthConfigError, AssertionError)


# T3: shape violations each raise, naming only the field, never the value.


def _auth_transport(auth: object) -> dict:
    return {
        "servers": [
            {
                "name": "home-bridge",
                "transport": {"kind": "streamable-http", "url": "https://home-bridge.local/mcp", "auth": auth},
            }
        ],
        "capabilities": [],
    }


def test_invalid_env_name_rejected() -> None:
    with pytest.raises(AuthConfigError, match="env"):
        RuntimeConfig.model_validate(_auth_transport({"header": "Authorization", "env": "123-not-valid"}))


def test_invalid_header_rejected() -> None:
    with pytest.raises(AuthConfigError, match="header"):
        RuntimeConfig.model_validate(_auth_transport({"header": "Bad Header Name", "env": "ALICE_MCP_TOKEN"}))


def test_crlf_header_rejected_without_echo() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        RuntimeConfig.model_validate(_auth_transport({"header": "X-Evil\r\nInjected: yes", "env": "ALICE_MCP_TOKEN"}))
    assert "Injected" not in str(excinfo.value)


@pytest.mark.parametrize("managed_header", ["content-type", "Accept", "Mcp-Session-Id", "mcp-protocol-version"])
def test_sdk_managed_header_rejected(managed_header: str) -> None:
    with pytest.raises(AuthConfigError, match="SDK-managed"):
        RuntimeConfig.model_validate(_auth_transport({"header": managed_header, "env": "ALICE_MCP_TOKEN"}))


def test_whitespace_scheme_rejected() -> None:
    with pytest.raises(AuthConfigError, match="scheme"):
        RuntimeConfig.model_validate(
            _auth_transport({"header": "Authorization", "scheme": "Bearer extra", "env": "ALICE_MCP_TOKEN"})
        )


def test_non_mapping_auth_rejected() -> None:
    with pytest.raises(AuthConfigError, match="mapping"):
        RuntimeConfig.model_validate(_auth_transport("not-a-mapping"))


def test_auth_on_stdio_transport_rejected() -> None:
    data = {
        "servers": [
            {
                "name": "hvac",
                "transport": {
                    "kind": "stdio",
                    "command": "hvac-server",
                    "auth": {"header": "Authorization", "scheme": "Bearer", "env": "ALICE_MCP_TOKEN"},
                },
            }
        ],
        "capabilities": [],
    }
    with pytest.raises(AuthConfigError, match="streamable-http"):
        RuntimeConfig.model_validate(data)


def test_auth_on_unknown_transport_kind_rejected_without_echo() -> None:
    data = {
        "servers": [
            {
                "name": "mystery",
                "transport": {
                    "kind": "carrier-pigeon",
                    "auth": {"header": "Authorization", "value": _SENTINEL},
                },
            }
        ],
        "capabilities": [],
    }
    with pytest.raises(AuthConfigError) as excinfo:
        RuntimeConfig.model_validate(data)
    assert _SENTINEL not in str(excinfo.value)


def test_direct_stdio_transport_validation_rejects_auth_without_echo() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        StdioTransport.model_validate(
            {"kind": "stdio", "command": "hvac-server", "auth": {"header": "Authorization", "value": _SENTINEL}}
        )
    assert _SENTINEL not in str(excinfo.value)


def test_direct_http_transport_validation_rejects_bad_auth_without_echo() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        HttpTransport.model_validate(
            {
                "kind": "streamable-http",
                "url": "https://home-bridge.local/mcp",
                "auth": {"header": "Authorization", "value": _SENTINEL},
            }
        )
    assert _SENTINEL not in str(excinfo.value)


def test_unrelated_invalid_field_does_not_echo_sibling_auth_secret() -> None:
    """An unrelated validation error (bad `url` type) must not surface the sibling secret."""
    data = {
        "servers": [
            {
                "name": "home-bridge",
                "transport": {
                    "kind": "streamable-http",
                    "url": 12345,  # wrong type, would normally raise a ValidationError
                    "auth": {"header": "Authorization", "value": _SENTINEL},
                },
            }
        ],
        "capabilities": [],
    }
    with pytest.raises(AuthConfigError) as excinfo:
        RuntimeConfig.model_validate(data)
    assert _SENTINEL not in str(excinfo.value)


# T4: fingerprint regression -- pinned to a literal captured from the
# pre-change code, proving no existing approval is invalidated.


def test_fingerprint_regression_no_auth_matches_pre_change_literal() -> None:
    server = _http_server("https://home-bridge.local/mcp")
    assert server.transport_fingerprint == "e5fcd0144bab2b5206ee970218b931acd89c44a5c960b0642fa9b60cd67c5bb5"
    assert server.binding_identity == (
        "home-bridge@sha256:e5fcd0144bab2b5206ee970218b931acd89c44a5c960b0642fa9b60cd67c5bb5"
    )


# T5: env/header/scheme identity vs. value.


def test_changing_auth_env_name_changes_fingerprint() -> None:
    a = _http_server_with_auth(env="ALICE_MCP_TOKEN")
    b = _http_server_with_auth(env="ALICE_MCP_TOKEN_V2")
    assert a.transport_fingerprint != b.transport_fingerprint


def test_changing_auth_header_changes_fingerprint() -> None:
    a = _http_server_with_auth(header="Authorization")
    b = _http_server_with_auth(header="X-Api-Key")
    assert a.transport_fingerprint != b.transport_fingerprint


def test_changing_auth_scheme_changes_fingerprint() -> None:
    a = _http_server_with_auth(scheme="Bearer")
    b = _http_server_with_auth(scheme=None)
    assert a.transport_fingerprint != b.transport_fingerprint


def test_auth_presence_changes_fingerprint_vs_no_auth() -> None:
    with_auth = _http_server_with_auth()
    without_auth = _http_server("https://home-bridge.local/mcp")
    assert with_auth.transport_fingerprint != without_auth.transport_fingerprint


def test_changing_only_env_value_does_not_change_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALICE_MCP_TOKEN", "value-one")
    a = _http_server_with_auth()
    before = a.transport_fingerprint
    monkeypatch.setenv("ALICE_MCP_TOKEN", "value-two")
    b = _http_server_with_auth()
    assert before == a.transport_fingerprint == b.transport_fingerprint


def test_fingerprint_computable_with_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALICE_MCP_TOKEN", raising=False)
    server = _http_server_with_auth()
    # No exception: transport_fingerprint never reads the environment.
    assert isinstance(server.transport_fingerprint, str)


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


@pytest.mark.parametrize("model,data", [
    (RuntimeConfig, [{"auth": {"token": _SENTINEL}}]),
    (RuntimeConfig, {"servers": {"auth": {"token": _SENTINEL}}}),
    (RuntimeConfig, {"oops": {"auth": {"token": _SENTINEL}}}),
    (HttpAuth, {"header": "Authorization", "env": "TOKEN", "value": _SENTINEL}),
    (HttpAuth, {"header": _SENTINEL + "\n", "env": "TOKEN"}),
    (ServerConfig, {"name": _SENTINEL, "transport": {"kind": "stdio", "auth": {"value": _SENTINEL}}}),
])
def test_malformed_auth_inputs_never_echo_secret(model, data) -> None:
    with pytest.raises(Exception) as caught:
        model.model_validate(data)
    assert _SENTINEL not in str(caught.value)
    assert _SENTINEL not in repr(caught.value)
    assert _SENTINEL not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("field,value", [
    ("header", "Authorization\n"), ("scheme", "Bearer\n"), ("env", "TOKEN\n"),
])
def test_auth_fields_reject_terminal_line_feed(field, value) -> None:
    auth = {"header": "Authorization", "scheme": "Bearer", "env": "TOKEN"}
    auth[field] = value
    with pytest.raises(AuthConfigError):
        HttpAuth.model_validate(auth)


def test_validated_auth_model_can_be_used_in_transport() -> None:
    auth = HttpAuth(header="Authorization", env="TOKEN")
    transport = HttpTransport(kind="streamable-http", url="https://example.test/mcp", auth=auth)
    assert transport.auth == auth


def test_malformed_yaml_diagnostic_does_not_echo_inline_secret(tmp_path) -> None:
    config = tmp_path / "runtime.yaml"
    secret = "YAML_LEAK"  # Short enough to appear in PyYAML's source excerpt.
    config.write_text("auth: {value: " + secret)
    with pytest.raises(ValueError, match="not parseable YAML") as caught:
        load_runtime_config(config)
    assert secret not in str(caught.value)
    assert secret not in "".join(traceback.format_exception(caught.value))


def test_capability_auth_argument_is_not_treated_as_transport_auth() -> None:
    data = {**MINIMAL_CONFIG, "capabilities": [{**MINIMAL_CONFIG["capabilities"][0], "arguments": {"auth": {"mode": "device"}}}]}
    config = RuntimeConfig.model_validate(data)
    assert config.capabilities[0].arguments["auth"] == {"mode": "device"}


def test_stdio_env_named_auth_is_not_a_transport_auth_block() -> None:
    config = RuntimeConfig(servers=(ServerConfig(name="device", transport={"kind": "stdio", "command": "device", "env": {"auth": "device-token"}}),))
    assert config.servers[0].transport.env["auth"] == "device-token"


def test_yaml_alias_cycle_does_not_recurse_in_auth_screen() -> None:
    cyclic = {}
    cyclic["extra"] = cyclic
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(cyclic)


def test_canonical_url_keeps_ipv6_brackets():
    from newton_mcp.runtime.config import _canonical_url

    assert _canonical_url("http://[::1]:8080/mcp") == "http://[::1]:8080/mcp"


def test_malformed_ipv6_url_rejected_without_echo():
    import pytest
    from pydantic import ValidationError

    from newton_mcp.runtime.config import HttpTransport

    with pytest.raises(ValidationError) as ei:
        HttpTransport(kind="streamable-http", url="http://user:sekret@[::1/mcp")
    assert "sekret" not in str(ei.value)
