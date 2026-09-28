from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from newton_mcp.runtime.config import (
    RUNTIME_CONFIG_ENV_VAR,
    RuntimeConfig,
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
