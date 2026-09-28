"""Runtime configuration, read from environment variables.

Variable names for the real backend follow Archetype's official conventions
(ATAI_API_KEY, ATAI_API_ENDPOINT). Everything else is project-specific.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Backend = Literal["mock", "api"]

DEFAULT_ENDPOINT = "https://api.u1.archetypeai.app/v0.5"
DEFAULT_TEXT_MODEL = "Newton::c2_5_8b_260413b723a9ab"
DEFAULT_OMEGA_MODEL = "OmegaEncoder::omega_embeddings_01"


@dataclass(frozen=True)
class Settings:
    backend: Backend = "mock"
    api_key: str | None = None
    api_endpoint: str = DEFAULT_ENDPOINT
    text_model: str = DEFAULT_TEXT_MODEL
    omega_model: str = DEFAULT_OMEGA_MODEL
    request_timeout_sec: float = 90.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        backend = env.get("NEWTON_BACKEND", "mock").strip().lower()
        if backend not in ("mock", "api"):
            raise ValueError(f"NEWTON_BACKEND must be 'mock' or 'api', got {backend!r}")
        api_key = env.get("ATAI_API_KEY") or None
        if backend == "api" and not api_key:
            raise ValueError("NEWTON_BACKEND=api requires ATAI_API_KEY")
        return cls(
            backend=backend,  # type: ignore[arg-type]
            api_key=api_key,
            api_endpoint=env.get("ATAI_API_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
            text_model=env.get("NEWTON_TEXT_MODEL", DEFAULT_TEXT_MODEL),
            omega_model=env.get("NEWTON_OMEGA_MODEL", DEFAULT_OMEGA_MODEL),
            request_timeout_sec=float(env.get("NEWTON_REQUEST_TIMEOUT_SEC", "90")),
        )
