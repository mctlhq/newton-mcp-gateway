"""Runtime configuration, read from environment variables.

Project-owned variables are namespaced `NEWTON_*` (e.g. `NEWTON_BACKEND`,
`NEWTON_MCP_TRANSPORT`, `NEWTON_MCP_HOST`, `NEWTON_MCP_PORT`). The bind
address prefers `NEWTON_MCP_HOST` / `NEWTON_MCP_PORT`, with the bare `HOST` /
`PORT` retained as a lower-precedence fallback so existing deployments keep
working; `HOST` in particular collides with a shell-reserved parameter, which
is why the prefixed name is preferred. Variable names for the real backend
follow Archetype's official conventions (ATAI_API_KEY, ATAI_API_ENDPOINT) and
stay unprefixed because Archetype's docs define them. Everything else is
project-specific.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Backend = Literal["mock", "api"]
Transport = Literal["stdio", "streamable-http"]

DEFAULT_ENDPOINT = "https://api.u1.archetypeai.app/v0.5"
DEFAULT_TEXT_MODEL = "Newton::c2_5_8b_260413b723a9ab"
DEFAULT_OMEGA_MODEL = "OmegaEncoder::omega_embeddings_01"

HOST_VARS = ("NEWTON_MCP_HOST", "HOST")
PORT_VARS = ("NEWTON_MCP_PORT", "PORT")

DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MiB; transport-shaped, not a documented API limit.
# The documented ceiling for POST /v0.5/files itself (docs.archetypeai.app/api-reference/files/upload).
DOCUMENTED_MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _resolve(env: dict[str, str], names: tuple[str, ...], default: str) -> tuple[str, str]:
    """Return (source_variable_name, stripped_value) for the first non-blank candidate.

    Blank and whitespace-only values are treated as "not supplied" so a blank
    prefixed variable falls through to the bare fallback and then to the default.
    When nothing is supplied, the first (preferred) name is reported as the source
    so that any downstream message names the variable an operator should set.
    """
    for name in names:
        raw = env.get(name)
        if raw is not None and raw.strip():
            return name, raw.strip()
    return names[0], default


@dataclass(frozen=True)
class Settings:
    backend: Backend = "mock"
    api_key: str | None = None
    api_endpoint: str = DEFAULT_ENDPOINT
    text_model: str = DEFAULT_TEXT_MODEL
    omega_model: str = DEFAULT_OMEGA_MODEL
    request_timeout_sec: float = 90.0
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        backend = env.get("NEWTON_BACKEND", "mock").strip().lower()
        if backend not in ("mock", "api"):
            raise ValueError(f"NEWTON_BACKEND must be 'mock' or 'api', got {backend!r}")
        api_key = env.get("ATAI_API_KEY") or None
        if backend == "api" and not api_key:
            raise ValueError("NEWTON_BACKEND=api requires ATAI_API_KEY")
        transport = env.get("NEWTON_MCP_TRANSPORT", "stdio").strip().lower()
        if transport not in ("stdio", "streamable-http"):
            raise ValueError(
                f"NEWTON_MCP_TRANSPORT must be 'stdio' or 'streamable-http', got {transport!r}"
            )
        _, host = _resolve(env, HOST_VARS, "127.0.0.1")
        port_var, port_raw = _resolve(env, PORT_VARS, "8000")
        try:
            port = int(port_raw)
        except ValueError:
            raise ValueError(f"{port_var} must be an integer, got {port_raw!r}") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"{port_var} must be in 1-65535, got {port}")
        max_image_bytes_raw = env.get("NEWTON_MAX_IMAGE_BYTES", str(DEFAULT_MAX_IMAGE_BYTES))
        try:
            max_image_bytes = int(max_image_bytes_raw)
        except ValueError:
            raise ValueError(
                f"NEWTON_MAX_IMAGE_BYTES must be an integer, got {max_image_bytes_raw!r}"
            ) from None
        if max_image_bytes <= 0:
            raise ValueError(f"NEWTON_MAX_IMAGE_BYTES must be > 0, got {max_image_bytes}")
        if max_image_bytes > DOCUMENTED_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"NEWTON_MAX_IMAGE_BYTES must be <= {DOCUMENTED_MAX_UPLOAD_BYTES} "
                f"(512 MiB, the documented Files API limit), got {max_image_bytes}"
            )
        return cls(
            backend=backend,  # type: ignore[arg-type]
            api_key=api_key,
            api_endpoint=env.get("ATAI_API_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
            text_model=env.get("NEWTON_TEXT_MODEL", DEFAULT_TEXT_MODEL),
            omega_model=env.get("NEWTON_OMEGA_MODEL", DEFAULT_OMEGA_MODEL),
            request_timeout_sec=float(env.get("NEWTON_REQUEST_TIMEOUT_SEC", "90")),
            transport=transport,  # type: ignore[arg-type]
            host=host,
            port=port,
            max_image_bytes=max_image_bytes,
        )
