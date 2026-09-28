"""Deterministic mock backend. Runs without credentials.

Every result is explicitly labelled ``backend="mock"`` and the text output
starts with ``[mock]`` so it can never be mistaken for real Newton output.
"""

from __future__ import annotations

import hashlib
import itertools
import math

from newton_mcp.newton.models import NewtonQueryRequest, NewtonQueryResult

OMEGA_DIM = 768  # matches the documented per-channel embedding size


class MockNewtonBackend:
    name = "mock"

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.requests: list[NewtonQueryRequest] = []

    async def query(self, request: NewtonQueryRequest) -> NewtonQueryResult:
        self.requests.append(request)
        query_id = f"mock-{next(self._counter):06d}"
        if request.model.startswith("OmegaEncoder::"):
            outputs = [self._embedding(ev.event_data.get("contents", [])) for ev in request.events
                       if ev.type == "data.numeric_array"]
            outputs = [vec for chan in outputs for vec in chan]
        else:
            n_events = len(request.events)
            n_files = len(request.file_ids)
            outputs = [
                f"[mock] Newton is not connected. Received query={request.query!r}, "
                f"events={n_events}, file_ids={n_files}. "
                "Set NEWTON_BACKEND=api with an authorized ATAI_API_KEY for real inference."
            ]
        return NewtonQueryResult(
            backend="mock",
            query_id=query_id,
            status="completed",
            model=request.model,
            outputs=outputs,
            inference_time_sec=0.0,
        )

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _embedding(channels: list[list[float]]) -> list[list[float]]:
        """One deterministic 768-dim pseudo-embedding per channel."""
        result: list[list[float]] = []
        for channel in channels:
            seed = hashlib.sha256(repr(channel).encode()).digest()
            base = int.from_bytes(seed[:8], "big")
            result.append([math.sin(base * 1e-9 + i * 0.01) for i in range(OMEGA_DIM)])
        return result
