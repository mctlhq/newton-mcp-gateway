"""Typed models mirroring the public Direct Query API (POST /query).

Field names intentionally match Archetype's documented request/response
shape so the adapter stays a thin, auditable mapping rather than a new
abstraction. Reference: docs.archetypeai.app/api-reference/query
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "data.text",
    "data.json",
    "data.base64_img",
    "data.base64_img_array",
    "data.numeric_array",
]


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
