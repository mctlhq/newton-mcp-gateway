import httpx
import pytest

from newton_mcp.newton.api import ArchetypeNewtonBackend, NewtonApiError
from newton_mcp.newton.models import ImageUpload, NewtonQueryRequest


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer t"})


async def test_parses_documented_success_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "query_id": "abc", "status": "completed", "inference_time_sec": 1.5,
            "response": {"success": True, "response": ["The pump is vibrating abnormally."]},
        })

    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=_client(handler))
    r = await b.query(NewtonQueryRequest(model="Newton::x", query="state?"))
    assert seen["url"] == "https://api.example/v0.5/query"
    assert seen["auth"] == "Bearer t"
    assert r.backend == "api" and r.query_id == "abc" and r.outputs == ["The pump is vibrating abnormally."]


async def test_error_envelope_raises():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": [{"code": "invalid_model_version", "message": "bad"}]})

    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=_client(handler))
    with pytest.raises(NewtonApiError) as ei:
        await b.query(NewtonQueryRequest(model="nope", query="q"))
    assert ei.value.status_code == 400
    assert ei.value.errors[0]["code"] == "invalid_model_version"


# --- T6: upload_image backend capability ------------------------------------


async def test_upload_image_posts_multipart_with_boundary_and_file_part():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"is_valid": True, "file_id": "img.png", "file_uid": "fil_x"})

    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=_client(handler))
    result = await b.upload_image(ImageUpload(data=b"\x89PNG\r\n", mime_type="image/png"))

    assert seen["url"] == "https://api.example/v0.5/files"
    assert seen["content_type"].startswith("multipart/form-data")
    assert "boundary=" in seen["content_type"]
    assert b'name="file"' in seen["body"]
    assert b'filename="image.png"' in seen["body"]
    assert result.backend == "api"
    assert result.file_id == "img.png"
    assert result.file_uid == "fil_x"


async def test_upload_image_raises_on_invalid_flag():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"is_valid": False, "file_id": "img.png"})

    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=_client(handler))
    with pytest.raises(NewtonApiError):
        await b.upload_image(ImageUpload(data=b"x", mime_type="image/png"))


async def test_upload_image_raises_on_non_200():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": [{"code": "bad_file"}]})

    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=_client(handler))
    with pytest.raises(NewtonApiError) as ei:
        await b.upload_image(ImageUpload(data=b"x", mime_type="image/png"))
    assert ei.value.status_code == 400


# --- T7: Content-Type header regression --------------------------------------


def test_client_built_without_injected_client_has_no_content_type_header():
    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5")
    assert "content-type" not in b._client.headers


async def test_query_still_sends_application_json_without_client_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={
            "query_id": "abc", "status": "completed",
            "response": {"success": True, "response": ["ok"]},
        })

    b = ArchetypeNewtonBackend("t", "https://api.example/v0.5", client=_client(handler))
    await b.query(NewtonQueryRequest(model="Newton::x", query="state?"))
    assert seen["content_type"].startswith("application/json")
