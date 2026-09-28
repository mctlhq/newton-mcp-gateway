import base64
import json

import httpx
import pytest

from newton_mcp.config import Settings
from newton_mcp.newton.api import ArchetypeNewtonBackend
from newton_mcp.newton.mock import MockNewtonBackend
from newton_mcp.server import create_server

from conftest import call_tool

TINY_PNG = base64.b64decode(
    # 1x1 transparent PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
TINY_PNG_B64 = base64.b64encode(TINY_PNG).decode()


# --- T2: mock inline path -------------------------------------------------


async def test_mock_inline_path_reports_decoded_bytes(mock_backend: MockNewtonBackend):
    settings = Settings()
    server = create_server(settings, backend=mock_backend)
    result = await call_tool(
        server, settings, mock_backend, "newton_analyze_image",
        {"question": "what is in this image?", "image_base64": TINY_PNG_B64, "mime_type": "image/png"},
    )
    payload = result.structured_content
    assert payload["backend"] == "mock"
    output = payload["outputs"][0]
    assert output.startswith("[mock]")
    assert "no image was analyzed" in output
    assert f"({len(TINY_PNG)} bytes decoded)" in output

    assert len(mock_backend.requests) == 1
    recorded = mock_backend.requests[0]
    assert len(recorded.events) == 1
    assert recorded.events[0].type == "data.base64_img"
    assert recorded.file_ids == []


async def test_mock_file_id_path_names_the_file_id(mock_backend: MockNewtonBackend):
    settings = Settings()
    server = create_server(settings, backend=mock_backend)
    result = await call_tool(
        server, settings, mock_backend, "newton_analyze_image",
        {"question": "what is in this image?", "file_id": "scene.jpeg"},
    )
    payload = result.structured_content
    output = payload["outputs"][0]
    assert output.startswith("[mock]")
    assert "no image was analyzed" in output
    assert "scene.jpeg" in output

    recorded = mock_backend.requests[0]
    assert recorded.file_ids == ["scene.jpeg"]
    assert recorded.events == []


# --- T3: API inline path ----------------------------------------------------


async def test_api_inline_path_single_canonical_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "query_id": "q1", "status": "completed",
            "response": {"success": True, "response": ["a description"]},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer t"})
    backend = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=client)
    settings = Settings()
    server = create_server(settings, backend=backend)

    prefixed = f"data:image/png;base64,{TINY_PNG_B64}"
    result = await call_tool(
        server, settings, backend, "newton_analyze_image",
        {"question": "what is it?", "image_base64": prefixed, "mime_type": "image/png"},
    )

    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.example/v0.5/query"
    body = json.loads(seen[0].content)
    assert body["events"] == [{"type": "data.base64_img", "event_data": {"contents": TINY_PNG_B64}}]
    assert body["file_ids"] == []
    assert "mime_type" not in body
    assert "mime_type" not in body["events"][0]["event_data"]

    payload = result.structured_content
    assert payload["backend"] == "api"


# --- T4: API file_id path ----------------------------------------------------


async def test_api_file_id_path_single_request_no_files_call():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "query_id": "q2", "status": "completed",
            "response": {"success": True, "response": ["a description"]},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer t"})
    backend = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=client)
    settings = Settings()
    server = create_server(settings, backend=backend)

    await call_tool(
        server, settings, backend, "newton_analyze_image",
        {"question": "what is it?", "file_id": "scene.PNG"},
    )

    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.example/v0.5/query"
    body = json.loads(seen[0].content)
    assert body["file_ids"] == ["scene.PNG"]
    assert body["events"] == []
    assert all("/files" not in str(r.url) for r in seen)


# --- T5: invalid input, zero requests ---------------------------------------


def _root_cause(exc: BaseException) -> BaseException:
    """Unwrap the framework's UnexpectedToolError wrapper to the raw ValueError
    the tool raised (see conftest.call_tool's docstring for why the wrapper's
    own str() doesn't carry the message)."""
    return exc.__cause__ if exc.__cause__ is not None else exc


@pytest.mark.parametrize(
    "kwargs,message_fragment",
    [
        ({"question": "q"}, "image_base64"),  # neither source
        ({"question": "q", "image_base64": "x", "file_id": "a.png"}, "image_base64"),  # both sources
        ({"question": "q", "image_base64": "aGVsbG8="}, "mime_type"),  # missing mime_type
        ({"question": "q", "image_base64": "aGVsbG8=", "mime_type": "image/gif"}, "mime_type"),  # bad mime_type
        ({"question": "q", "image_base64": "not-base64!!!", "mime_type": "image/png"}, "base64"),  # garbage
        ({"question": "q", "file_id": "fil_abc"}, "file_id"),  # file_uid, not file_id
        ({"question": "q", "file_id": "notes.csv"}, "file_id"),  # no image extension
    ],
)
async def test_invalid_input_raises_before_any_request(mock_backend: MockNewtonBackend, kwargs, message_fragment):
    settings = Settings()
    server = create_server(settings, backend=mock_backend)
    with pytest.raises(Exception) as ei:
        await call_tool(server, settings, mock_backend, "newton_analyze_image", kwargs)
    cause = _root_cause(ei.value)
    assert isinstance(cause, ValueError)
    assert message_fragment in str(cause)
    assert mock_backend.requests == []


async def test_oversize_image_rejected_before_request(mock_backend: MockNewtonBackend):
    settings = Settings(max_image_bytes=4)
    server = create_server(settings, backend=mock_backend)
    with pytest.raises(Exception) as ei:
        await call_tool(
            server, settings, mock_backend, "newton_analyze_image",
            {"question": "q", "image_base64": TINY_PNG_B64, "mime_type": "image/png"},
        )
    cause = _root_cause(ei.value)
    assert isinstance(cause, ValueError)
    # TINY_PNG's encoding is far past the 8-character bound for a 4-byte limit, so the
    # pre-decode length check rejects it; the post-decode check has its own test below.
    assert str(len(TINY_PNG_B64)) in str(cause)
    assert "4 byte limit" in str(cause)
    assert "NEWTON_MAX_IMAGE_BYTES" in str(cause)
    assert mock_backend.requests == []


async def test_invalid_base64_error_does_not_echo_payload(mock_backend: MockNewtonBackend):
    settings = Settings()
    server = create_server(settings, backend=mock_backend)
    garbage = "not-base64-garbage-payload-xyz!!!"
    with pytest.raises(Exception) as ei:
        await call_tool(
            server, settings, mock_backend, "newton_analyze_image",
            {"question": "q", "image_base64": garbage, "mime_type": "image/png"},
        )
    cause = _root_cause(ei.value)
    assert isinstance(cause, ValueError)
    assert garbage not in str(cause)


# --- P2 regression: encoded-length bound runs before decoding -----------------


async def test_oversize_payload_rejected_before_decoding(mock_backend: MockNewtonBackend, monkeypatch):
    import newton_mcp.server as server_module

    def _no_decode(*args, **kwargs):
        raise AssertionError("b64decode must not run for an oversized payload")

    monkeypatch.setattr(server_module.base64, "b64decode", _no_decode)
    settings = Settings(max_image_bytes=6)
    server = create_server(settings, backend=mock_backend)
    payload = "A" * 12  # 12 chars > 4 * ceil(6 / 3) == 8
    with pytest.raises(Exception) as ei:
        await call_tool(
            server, settings, mock_backend, "newton_analyze_image",
            {"question": "q", "image_base64": payload, "mime_type": "image/png"},
        )
    cause = _root_cause(ei.value)
    assert isinstance(cause, ValueError)
    assert "NEWTON_MAX_IMAGE_BYTES" in str(cause)
    assert payload not in str(cause)
    assert mock_backend.requests == []


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 7])
async def test_payload_at_exact_limit_passes_pre_decode_bound(mock_backend: MockNewtonBackend, limit):
    settings = Settings(max_image_bytes=limit)
    server = create_server(settings, backend=mock_backend)
    payload = base64.b64encode(b"\xff" * limit).decode()
    result = await call_tool(
        server, settings, mock_backend, "newton_analyze_image",
        {"question": "q", "image_base64": payload, "mime_type": "image/png"},
    )
    assert result.structured_content["backend"] == "mock"
    assert f"({limit} bytes decoded)" in result.structured_content["outputs"][0]
    assert len(mock_backend.requests) == 1


@pytest.mark.parametrize("limit", [4, 5, 7])
async def test_one_byte_over_limit_caught_by_post_decode_check(mock_backend: MockNewtonBackend, limit):
    # limit + 1 bytes still fits in 4 * ceil(limit / 3) characters for these limits,
    # so only the post-decode check can reject it.
    settings = Settings(max_image_bytes=limit)
    server = create_server(settings, backend=mock_backend)
    payload = base64.b64encode(b"\xff" * (limit + 1)).decode()
    assert len(payload) <= 4 * ((limit + 2) // 3)
    with pytest.raises(Exception) as ei:
        await call_tool(
            server, settings, mock_backend, "newton_analyze_image",
            {"question": "q", "image_base64": payload, "mime_type": "image/png"},
        )
    cause = _root_cause(ei.value)
    assert isinstance(cause, ValueError)
    assert f"decoded image is {limit + 1} bytes" in str(cause)
    assert mock_backend.requests == []
