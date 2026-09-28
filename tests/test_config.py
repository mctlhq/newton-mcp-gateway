import pytest

from newton_mcp.config import Settings


def test_defaults_to_mock():
    s = Settings.from_env({})
    assert s.backend == "mock"
    assert s.api_endpoint.endswith("/v0.5")


def test_api_requires_key():
    with pytest.raises(ValueError):
        Settings.from_env({"NEWTON_BACKEND": "api"})


def test_api_reads_official_env_names():
    s = Settings.from_env({"NEWTON_BACKEND": "api", "ATAI_API_KEY": "k", "ATAI_API_ENDPOINT": "https://x/v0.5/"})
    assert s.api_key == "k"
    assert s.api_endpoint == "https://x/v0.5"


def test_transport_and_network_defaults():
    s = Settings.from_env({})
    assert s.transport == "stdio"
    assert s.host == "127.0.0.1"
    assert s.port == 8000


def test_transport_is_normalized():
    s = Settings.from_env({"NEWTON_MCP_TRANSPORT": " Streamable-HTTP "})
    assert s.transport == "streamable-http"
    s = Settings.from_env({"NEWTON_MCP_TRANSPORT": "stdio"})
    assert s.transport == "stdio"


def test_invalid_transport_raises():
    with pytest.raises(ValueError, match="NEWTON_MCP_TRANSPORT"):
        Settings.from_env({"NEWTON_MCP_TRANSPORT": "websocket"})


def test_host_and_port_overrides():
    s = Settings.from_env({"HOST": "0.0.0.0", "PORT": "9001"})
    assert s.host == "0.0.0.0"
    assert s.port == 9001
    assert isinstance(s.port, int)
    s = Settings.from_env({"HOST": ""})
    assert s.host == "127.0.0.1"


def test_invalid_port_raises():
    with pytest.raises(ValueError, match="PORT"):
        Settings.from_env({"PORT": "eighty"})
    with pytest.raises(ValueError, match="PORT"):
        Settings.from_env({"PORT": "0"})
    with pytest.raises(ValueError, match="PORT"):
        Settings.from_env({"PORT": "70000"})


def test_prefixed_host_and_port_win_over_bare():
    s = Settings.from_env(
        {
            "NEWTON_MCP_HOST": "0.0.0.0",
            "HOST": "evil.example",
            "NEWTON_MCP_PORT": "9100",
            "PORT": "1",
        }
    )
    assert s.host == "0.0.0.0"
    assert s.port == 9100


def test_prefixed_host_and_port_alone():
    s = Settings.from_env({"NEWTON_MCP_HOST": "0.0.0.0", "NEWTON_MCP_PORT": "9001"})
    assert s.host == "0.0.0.0"
    assert s.port == 9001
    assert isinstance(s.port, int)


def test_bare_host_and_port_still_work():
    s = Settings.from_env({"HOST": "0.0.0.0", "PORT": "9001"})
    assert s.host == "0.0.0.0"
    assert s.port == 9001


def test_blank_prefixed_falls_through_to_bare():
    s = Settings.from_env(
        {
            "NEWTON_MCP_HOST": "  ",
            "HOST": "0.0.0.0",
            "NEWTON_MCP_PORT": "",
            "PORT": "9002",
        }
    )
    assert s.host == "0.0.0.0"
    assert s.port == 9002


def test_blank_everywhere_uses_defaults():
    s = Settings.from_env(
        {"NEWTON_MCP_HOST": "", "HOST": "", "NEWTON_MCP_PORT": "", "PORT": ""}
    )
    assert s.host == "127.0.0.1"
    assert s.port == 8000


def test_port_error_names_the_supplying_variable():
    with pytest.raises(ValueError, match=r"^NEWTON_MCP_PORT must be an integer"):
        Settings.from_env({"NEWTON_MCP_PORT": "eighty"})
    with pytest.raises(ValueError, match=r"^PORT must be an integer"):
        Settings.from_env({"PORT": "eighty"})
    with pytest.raises(ValueError, match=r"^NEWTON_MCP_PORT must be in 1-65535"):
        Settings.from_env({"NEWTON_MCP_PORT": "0"})
    with pytest.raises(ValueError, match=r"^NEWTON_MCP_PORT must be in 1-65535"):
        Settings.from_env({"NEWTON_MCP_PORT": "70000"})


def test_prefixed_port_error_wins_over_valid_bare():
    with pytest.raises(ValueError, match=r"^NEWTON_MCP_PORT"):
        Settings.from_env({"NEWTON_MCP_PORT": "nope", "PORT": "8000"})
