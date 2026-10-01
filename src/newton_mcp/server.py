"""MCP server exposing Newton as a capability (Direction B).

Four tools on purpose. More is not better: each tool maps to a documented
/query pattern and returns structured, auditable output.
"""

from __future__ import annotations

import base64
import binascii
import functools
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from newton_mcp import __version__
from newton_mcp.action.propose import propose_action
from newton_mcp.config import Settings
from newton_mcp.errors import InputValidationError
from newton_mcp.newton.api import build_backend
from newton_mcp.newton.models import IMAGE_FILE_EXTENSIONS, IMAGE_MIME_EXTENSIONS, DataEvent, ImageMimeType, NewtonQueryRequest
from newton_mcp.newton.protocol import NewtonBackend


_T = TypeVar("_T")


def _surface_input_errors(fn: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
    """Translate only `InputValidationError` into an SDK `ToolError`.

    Any other exception (including a backend `ValueError`) is left alone so the
    SDK keeps reporting it as a generic failure without exposing its message.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return await fn(*args, **kwargs)
        except InputValidationError as exc:
            raise ToolError(str(exc)) from None

    return wrapper


@dataclass
class AppState:
    settings: Settings
    backend: NewtonBackend


def create_server(settings: Settings | None = None, backend: NewtonBackend | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    backend = backend or build_backend(settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppState]:
        try:
            yield AppState(settings=settings, backend=backend)
        finally:
            await backend.aclose()

    server = MCPServer(
        name="newton-mcp-gateway",
        version=__version__,
        instructions=(
            "Experimental bridge to Archetype AI Newton, a foundation model for physical sensor data. "
            f"Backend: {backend.name}. Mock results are labelled and are not real inference."
        ),
        lifespan=lifespan,
    )

    def _state(ctx: Context) -> AppState:
        return ctx.request_context.lifespan_context  # type: ignore[return-value]

    @server.tool(
        name="newton_query",
        description=(
            "Ask Newton (text-reasoning model) a natural-language question about physical-world data. "
            "Ground it with inline text/JSON events or previously uploaded file_ids. "
            "Use system_prompt to force structured JSON output."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @_surface_input_errors
    async def newton_query(
        ctx: Context,
        query: str,
        system_prompt: str = "",
        text_events: list[str] | None = None,
        json_events: list[str] | None = None,
        file_ids: list[str] | None = None,
        max_new_tokens: int = 400,
        model: str | None = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        events = [DataEvent.text(t) for t in (text_events or [])]
        events += [DataEvent(type="data.json", event_data={"contents": j}) for j in (json_events or [])]
        request = NewtonQueryRequest(
            model=model or state.settings.text_model,
            query=query,
            system_prompt=system_prompt,
            instruction_prompt=system_prompt,
            events=events,
            file_ids=file_ids or [],
            max_new_tokens=max_new_tokens,
        )
        result = await state.backend.query(request)
        return result.model_dump(exclude={"raw"})

    @server.tool(
        name="newton_embed_timeseries",
        description=(
            "Encode a sensor window with the Newton Omega encoder. Input is channel-first: "
            "outer list = channels, inner lists = samples. Returns one 768-dim embedding per channel. "
            "Leave normalize=false unless cross-window amplitude is irrelevant."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @_surface_input_errors
    async def newton_embed_timeseries(
        ctx: Context,
        channels: list[list[float]],
        normalize: bool = False,
        model: str | None = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        if not channels or any(len(c) == 0 for c in channels):
            raise InputValidationError("channels must be a non-empty list of non-empty sample lists")
        request = NewtonQueryRequest(
            model=model or state.settings.omega_model,
            query="",
            events=[DataEvent.numeric_array(channels)],
            normalize_input=normalize,
        )
        result = await state.backend.query(request)
        payload = result.model_dump(exclude={"raw"})
        payload["embedding_dims"] = [len(v) for v in result.outputs]
        return payload

    @server.tool(
        name="newton_analyze_image",
        description=(
            "Ask Newton C a natural-language question about exactly one image. "
            "Provide exactly one of: image_base64 (with mime_type image/png or image/jpeg), "
            "sent inline as a data.base64_img event; or file_id, an existing "
            "Files API file_id (must end in .png/.jpg/.jpeg). This tool never uploads or "
            "stores anything -- it makes one read-only call to /query."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @_surface_input_errors
    async def newton_analyze_image(
        ctx: Context,
        question: str,
        image_base64: str | None = None,
        mime_type: ImageMimeType | None = None,
        file_id: str | None = None,
        system_prompt: str = "",
        max_new_tokens: int = 400,
        model: str | None = None,
    ) -> dict[str, Any]:
        state = _state(ctx)

        if (image_base64 is None) == (file_id is None):
            raise InputValidationError(
                "exactly one of image_base64 (with mime_type) or file_id is required"
            )

        if image_base64 is not None:
            accepted = sorted(IMAGE_MIME_EXTENSIONS)
            if mime_type is None or mime_type not in IMAGE_MIME_EXTENSIONS:
                raise InputValidationError(
                    f"mime_type is required with image_base64 and must be one of {accepted}, "
                    f"got {mime_type!r}"
                )
            payload = image_base64
            if payload.startswith("data:") and ";base64," in payload:
                payload = payload.split(";base64,", 1)[1]
            # Bound the encoded length before decoding so an oversized payload is
            # rejected without being materialised in memory. Strict (validate=True)
            # base64 of n bytes is exactly 4 * ceil(n / 3) characters, so any valid
            # payload longer than that decodes to more than max_image_bytes.
            max_bytes = state.settings.max_image_bytes
            max_encoded_len = 4 * ((max_bytes + 2) // 3)
            if len(payload) > max_encoded_len:
                raise InputValidationError(
                    f"image_base64 is {len(payload)} characters, longer than the {max_encoded_len} "
                    f"characters that can encode the {max_bytes} byte limit; "
                    "raise NEWTON_MAX_IMAGE_BYTES to allow larger images"
                )
            try:
                raw = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise InputValidationError(f"image_base64 is not valid base64: {exc}") from None
            if len(raw) == 0:
                raise InputValidationError("image_base64 decodes to zero bytes; provide a non-empty image")
            if len(raw) > max_bytes:
                raise InputValidationError(
                    f"decoded image is {len(raw)} bytes, exceeding the {max_bytes} "
                    "byte limit; raise NEWTON_MAX_IMAGE_BYTES to allow larger images"
                )
            events = [DataEvent.base64_img(base64.b64encode(raw).decode())]
            file_ids: list[str] = []
        else:
            assert file_id is not None
            if not file_id.lower().endswith(IMAGE_FILE_EXTENSIONS):
                raise InputValidationError(
                    f"file_id must end in one of {IMAGE_FILE_EXTENSIONS} (the documented "
                    "extension-bearing file_id, not a file_uid, since /query filters files "
                    "by extension)"
                )
            events = []
            file_ids = [file_id]

        request = NewtonQueryRequest(
            model=model or state.settings.text_model,
            query=question,
            system_prompt=system_prompt,
            instruction_prompt=system_prompt,
            events=events,
            file_ids=file_ids,
            max_new_tokens=max_new_tokens,
        )
        result = await state.backend.query(request)
        return result.model_dump(exclude={"raw"})

    @server.tool(
        name="newton_propose_action",
        description=(
            "Propose exactly one validated Physical Action Contract from a physical-world "
            "observation, by calling Newton C with a strict JSON system prompt. Returns either "
            "a schema-valid contract or a failure with the raw model text and validation errors "
            "-- never a guessed, defaulted or partially filled contract. This tool only "
            "proposes an action; it never executes or authorises one."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @_surface_input_errors
    async def newton_propose_action(
        ctx: Context,
        text_events: list[str] | None = None,
        json_events: list[str] | None = None,
        allowed_goals: list[str] | None = None,
        observation_id: str | None = None,
        max_new_tokens: int = 700,
        model: str | None = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        result = await propose_action(
            state.backend,
            model=model or state.settings.text_model,
            text_events=text_events or [],
            json_events=json_events or [],
            allowed_goals=allowed_goals,
            observation_id=observation_id,
            max_new_tokens=max_new_tokens,
        )
        return result.model_dump()

    return server


def main() -> None:
    settings = Settings.from_env()
    server = create_server(settings)
    if settings.transport == "streamable-http":
        server.run(transport="streamable-http", host=settings.host, port=settings.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
