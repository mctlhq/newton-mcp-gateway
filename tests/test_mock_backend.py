from newton_mcp.newton.mock import OMEGA_DIM, MockNewtonBackend
from newton_mcp.newton.models import DataEvent, ImageUpload, NewtonQueryRequest


async def test_text_query_is_clearly_mock(mock_backend: MockNewtonBackend):
    r = await mock_backend.query(NewtonQueryRequest(model="Newton::x", query="state?"))
    assert r.backend == "mock"
    assert r.status == "completed"
    assert r.outputs[0].startswith("[mock]")


async def test_omega_returns_one_vector_per_channel(mock_backend: MockNewtonBackend):
    req = NewtonQueryRequest(model="OmegaEncoder::x", events=[DataEvent.numeric_array([[1, 2, 3], [4, 5, 6]])])
    r = await mock_backend.query(req)
    assert len(r.outputs) == 2
    assert all(len(v) == OMEGA_DIM for v in r.outputs)


def test_payload_matches_documented_shape():
    req = NewtonQueryRequest(model="m", query="q", events=[DataEvent.text("hello")], file_ids=["a.png"])
    p = req.to_payload()
    assert p["model"] == "m" and p["file_ids"] == ["a.png"]
    assert p["events"] == [{"type": "data.text", "event_data": {"contents": "hello"}}]
    assert p["sanitize_response"] is False


async def test_mock_upload_image_is_deterministic_and_obviously_fake(mock_backend: MockNewtonBackend):
    a = await mock_backend.upload_image(ImageUpload(data=b"x", mime_type="image/png"))
    b = await mock_backend.upload_image(ImageUpload(data=b"y", mime_type="image/jpeg"))
    assert a.backend == "mock" and a.file_id == "mock-image-000001.png"
    assert b.backend == "mock" and b.file_id == "mock-image-000002.jpg"
