"""Real adapter for the Archetype Direct Query API.

Only publicly documented behaviour is used (POST {ATAI_API_ENDPOINT}/query with
a Bearer token). Not validated against a live account yet — see README status.
"""

from __future__ import annotations

from typing import Any

import httpx

from newton_mcp.newton.models import NewtonQueryRequest, NewtonQueryResult


class NewtonApiError(RuntimeError):
    def __init__(self, status_code: int, errors: Any) -> None:
        super().__init__(f"Newton API error {status_code}: {errors}")
        self.status_code = status_code
        self.errors = errors


class ArchetypeNewtonBackend:
    name = "api"

    def __init__(self, api_key: str, endpoint: str, timeout_sec: float = 90.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout_sec,
        )

    async def query(self, request: NewtonQueryRequest) -> NewtonQueryResult:
        resp = await self._client.post(f"{self._endpoint}/query", json=request.to_payload())
        body = self._json(resp)
        if resp.status_code == 401:
            raise NewtonApiError(401, body.get("detail", "unauthorized"))
        if resp.status_code != 200:
            raise NewtonApiError(resp.status_code, body.get("errors", body))
        inner = body.get("response") or {}
        status = body.get("status", "failed")
        return NewtonQueryResult(
            backend="api",
            query_id=str(body.get("query_id", "")),
            status="completed" if status == "completed" else "failed",
            model=request.model,
            outputs=list(inner.get("response") or []),
            inference_time_sec=body.get("inference_time_sec"),
            error=body.get("error_msg"),
            raw=body,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {"errors": resp.text}
        return data if isinstance(data, dict) else {"errors": data}


def build_backend(settings) -> "ArchetypeNewtonBackend | Any":
    """Factory used by the server: picks the backend from Settings."""
    from newton_mcp.newton.mock import MockNewtonBackend

    if settings.backend == "api":
        return ArchetypeNewtonBackend(settings.api_key, settings.api_endpoint, settings.request_timeout_sec)
    return MockNewtonBackend()
