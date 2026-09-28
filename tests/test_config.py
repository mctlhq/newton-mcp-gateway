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
