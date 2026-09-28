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
