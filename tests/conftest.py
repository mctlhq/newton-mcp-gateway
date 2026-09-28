import pytest

from newton_mcp.config import Settings
from newton_mcp.newton.mock import MockNewtonBackend
from newton_mcp.server import create_server


@pytest.fixture
def mock_backend() -> MockNewtonBackend:
    return MockNewtonBackend()


@pytest.fixture
def server(mock_backend):
    return create_server(Settings(), backend=mock_backend)
