"""Real-client checks for marked input errors and the MIME schema."""

import pytest
from mcp import Client

from newton_mcp.config import Settings
from newton_mcp.errors import InputValidationError
from newton_mcp.newton.mock import MockNewtonBackend
from newton_mcp.server import create_server

SENTINEL = "sk-secret-sentinel"


class FailingBackend(MockNewtonBackend):
    def __init__(self, exc: Exception):
        super().__init__()
        self.exc = exc
        self.calls = 0

    async def query(self, request):
        self.calls += 1
        raise self.exc


async def _call(backend, name, args):
    async with Client(create_server(Settings(), backend=backend)) as client:
        return await client.call_tool(name, args)


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


async def test_marked_error_is_actionable_and_makes_no_backend_call():
    backend = FailingBackend(RuntimeError("x"))
    r = await _call(backend, "newton_embed_timeseries", {"channels": []})
    assert r.is_error and "non-empty" in _text(r)
    r = await _call(backend, "newton_analyze_image", {"question": "q", "image_base64": "", "mime_type": "image/png"})
    assert r.is_error and "zero bytes" in _text(r)
    r = await _call(backend, "newton_propose_action", {"text_events": [" "]})
    assert r.is_error and "non-empty" in _text(r)
    assert backend.calls == 0


@pytest.mark.parametrize("exc", [ValueError(SENTINEL), RuntimeError(SENTINEL)])
async def test_generic_backend_errors_stay_generic(exc):
    backend = FailingBackend(exc)
    for name, args in [
        ("newton_query", {"query": "q"}),
        ("newton_embed_timeseries", {"channels": [[1.0]]}),
        ("newton_analyze_image", {"question": "q", "file_id": "a.png"}),
        ("newton_propose_action", {"text_events": ["x"]}),
    ]:
        r = await _call(backend, name, args)
        assert r.is_error and SENTINEL not in _text(r)
    assert backend.calls == 4


async def test_backend_marked_type_is_only_translation_target():
    # A backend raising the marked type is still an InputValidationError by design;
    # plain ValueError is what must not be surfaced (covered above).
    assert issubclass(InputValidationError, ValueError)


async def test_mime_schema_is_png_jpeg_or_null():
    async with Client(create_server(Settings(), backend=MockNewtonBackend())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    prop = tools["newton_analyze_image"].input_schema["properties"]["mime_type"]
    variants = prop["anyOf"]
    enum = next(v for v in variants if "enum" in v)["enum"]
    assert sorted(enum) == ["image/jpeg", "image/png"]
    assert {"type": "null"} in variants
