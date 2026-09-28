import httpx
import pytest

from newton_mcp.newton.api import ArchetypeNewtonBackend, NewtonApiError
from newton_mcp.newton.models import NewtonQueryRequest


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
