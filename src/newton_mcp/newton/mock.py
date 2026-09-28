"""Deterministic mock backend. Runs without credentials.

Every result is explicitly labelled ``backend="mock"`` and the text output
starts with ``[mock]`` so it can never be mistaken for real Newton output.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import math

from newton_mcp.newton.models import (
    IMAGE_FILE_EXTENSIONS,
    IMAGE_MIME_EXTENSIONS,
    ImageUpload,
    NewtonQueryRequest,
    NewtonQueryResult,
    UploadedFile,
)

OMEGA_DIM = 768  # matches the documented per-channel embedding size


class MockNewtonBackend:
    name = "mock"

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._upload_counter = itertools.count(1)
        self.requests: list[NewtonQueryRequest] = []

    async def query(self, request: NewtonQueryRequest) -> NewtonQueryResult:
        self.requests.append(request)
        query_id = f"mock-{next(self._counter):06d}"
        image_event = next((ev for ev in request.events if ev.type == "data.base64_img"), None)
        image_file_id = next(
            (fid for fid in request.file_ids if fid.lower().endswith(IMAGE_FILE_EXTENSIONS)), None
        )
        if request.model.startswith("OmegaEncoder::"):
            outputs = [self._embedding(ev.event_data.get("contents", [])) for ev in request.events
                       if ev.type == "data.numeric_array"]
            outputs = [vec for chan in outputs for vec in chan]
        elif image_event is not None:
            try:
                n_bytes = len(base64.b64decode(image_event.event_data.get("contents", ""), validate=True))
            except (binascii.Error, ValueError):
                n_bytes = 0
            outputs = [
                "[mock] Newton is not connected and no image was analyzed. "
                f"Received a data.base64_img event ({n_bytes} bytes decoded) and "
                f"question={request.query!r}. "
                "Set NEWTON_BACKEND=api with an authorized ATAI_API_KEY for real inference."
            ]
        elif image_file_id is not None:
            outputs = [
                "[mock] Newton is not connected and no image was analyzed. "
                f"Received file_id={image_file_id!r} and question={request.query!r}. "
                "Set NEWTON_BACKEND=api with an authorized ATAI_API_KEY for real inference."
            ]
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

    async def upload_image(self, image: ImageUpload) -> UploadedFile:
        ext = IMAGE_MIME_EXTENSIONS[image.mime_type]
        n = next(self._upload_counter)
        return UploadedFile(backend="mock", file_id=f"mock-image-{n:06d}{ext}", file_uid=f"fil_mock{n:06d}")

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
