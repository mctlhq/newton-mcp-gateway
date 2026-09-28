from __future__ import annotations

from typing import Protocol, runtime_checkable

from newton_mcp.newton.models import ImageUpload, NewtonQueryRequest, NewtonQueryResult, UploadedFile


@runtime_checkable
class NewtonBackend(Protocol):
    """The single seam between this gateway and Newton.

    Implementations: ``MockNewtonBackend`` (always available) and
    ``ArchetypeNewtonBackend`` (requires authorized ATAI_API_KEY).
    """

    name: str

    async def query(self, request: NewtonQueryRequest) -> NewtonQueryResult: ...

    async def upload_image(self, image: ImageUpload) -> UploadedFile: ...

    async def aclose(self) -> None: ...
