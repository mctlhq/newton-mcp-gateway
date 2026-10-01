"""Typed models mirroring the public Direct Query API (POST /query).

Field names intentionally match Archetype's documented request/response
shape so the adapter stays a thin, auditable mapping rather than a new
abstraction. Reference: docs.archetypeai.app/api-reference/query
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ImageMimeType = Literal["image/png", "image/jpeg"]

EventType = Literal[
    "data.text",
    "data.json",
    "data.base64_img",
    "data.base64_img_array",
    "data.numeric_array",
]

# image/png, image/jpeg -> file extension used when minting a Files API filename.
# Reference: docs.archetypeai.app/api-reference/files/upload
IMAGE_MIME_EXTENSIONS: dict[ImageMimeType, str] = {"image/png": ".png", "image/jpeg": ".jpg"}

# Extensions /query recognizes as an image file_id (case-insensitive match at the
# call site). Reference: docs.archetypeai.app/api-reference/query
IMAGE_FILE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg")


class DataEvent(BaseModel):
    """Inline data event passed in place of a file upload.

    For ``data.json`` the ``contents`` value must be a serialized JSON string
    (the API rejects a parsed object). For ``data.numeric_array`` it is a
    channel-first list of lists: outer = channels, inner = samples.
    """

    type: EventType
    event_data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text(cls, contents: str) -> "DataEvent":
        return cls(type="data.text", event_data={"contents": contents})

    @classmethod
    def numeric_array(cls, contents: list[list[float]]) -> "DataEvent":
        return cls(type="data.numeric_array", event_data={"contents": contents})

    @classmethod
    def base64_img(cls, b64: str) -> "DataEvent":
        """One inline image, per the Data Events page's documented ``data.base64_img``
        shape: ``event_data.contents`` is the base64-encoded image as a byte string."""
        return cls(type="data.base64_img", event_data={"contents": b64})


class NewtonQueryRequest(BaseModel):
    model: str
    query: str = ""
    system_prompt: str = ""
    instruction_prompt: str = ""
    file_ids: list[str] = Field(default_factory=list)
    events: list[DataEvent] = Field(default_factory=list)
    max_new_tokens: int = 256
    normalize_input: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True)
        # The API expects sanitized responses by default; we ask for the raw
        # record so query_id / timings are preserved for audit trails.
        payload["sanitize_response"] = False
        return payload


class NewtonQueryResult(BaseModel):
    """Normalized result. ``outputs`` is ``response.response`` from the API:
    strings for text models, per-channel embedding vectors for Omega."""

    backend: Literal["mock", "api"]
    query_id: str
    status: Literal["completed", "failed"]
    model: str
    outputs: list[Any]
    inference_time_sec: float | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None


class ImageUpload(BaseModel):
    """Input to the documented ``POST /v0.5/files`` multipart upload.

    Not reachable from any MCP tool in this repo yet -- a backend capability
    only. Reference: docs.archetypeai.app/api-reference/files/upload
    """

    data: bytes
    mime_type: Literal["image/png", "image/jpeg"]

    @property
    def filename(self) -> str:
        return f"image{IMAGE_MIME_EXTENSIONS[self.mime_type]}"


class UploadedFile(BaseModel):
    """Normalized response from the Files API upload.

    ``file_id`` is the extension-bearing name to pass back into ``file_ids``
    on ``/query``; ``file_uid`` is kept only as data (the API rejects it in
    place of ``file_id``).
    """

    backend: Literal["mock", "api"]
    file_id: str
    file_uid: str | None = None
