"""`runtime.yaml`: the reviewable allow-list of MCP server + tool capabilities.

Every model forbids unknown keys (`extra="forbid"`) on purpose: a misspelt
key in an actuator allow-list must fail loudly at load time, never silently
widen what a physical action can reach. See docs/action-runtime.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

RUNTIME_CONFIG_ENV_VAR = "NEWTON_MCP_RUNTIME_CONFIG"


class StdioTransport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["stdio"]
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)


class HttpTransport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["streamable-http"]
    url: str


Transport = Annotated[StdioTransport | HttpTransport, Field(discriminator="kind")]


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Transport
    identity: str | None = None

    @property
    def resolved_identity(self) -> str:
        """The identity `CandidateAction.server_identity` names; defaults to `name`."""
        return self.identity if self.identity is not None else self.name


class TargetMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    locations: tuple[str, ...] = ()
    """Case-insensitive exact match against `contract.target.location`. Empty is a wildcard."""


class CapabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str
    tool: str
    goal_prefixes: tuple[str, ...] = Field(min_length=1)
    target: TargetMatch
    arguments: dict[str, Any] = Field(default_factory=dict)
    read_tool: str | None = None
    idempotent: bool = False


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servers: tuple[ServerConfig, ...] = ()
    capabilities: tuple[CapabilityConfig, ...] = ()

    @property
    def servers_by_name(self) -> dict[str, ServerConfig]:
        return {server.name: server for server in self.servers}

    @model_validator(mode="after")
    def _validate_references(self) -> "RuntimeConfig":
        names = [server.name for server in self.servers]
        name_dupes = _duplicates(names)
        if name_dupes:
            raise ValueError(f"duplicate server name(s): {sorted(name_dupes)}")

        identities = [server.resolved_identity for server in self.servers]
        identity_dupes = _duplicates(identities)
        if identity_dupes:
            raise ValueError(
                f"duplicate resolved server identity(ies): {sorted(identity_dupes)} "
                "(CandidateAction.server_identity must name exactly one server)"
            )

        known_servers = set(names)
        seen_pairs: set[tuple[str, str]] = set()
        for capability in self.capabilities:
            if capability.server not in known_servers:
                raise ValueError(
                    f"capability (server={capability.server!r}, tool={capability.tool!r}) "
                    f"references undeclared server {capability.server!r}"
                )
            pair = (capability.server, capability.tool)
            if pair in seen_pairs:
                raise ValueError(f"duplicate capability (server, tool) pair: {pair!r}")
            seen_pairs.add(pair)

        return self


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes


def load_runtime_config(path: str | Path | None = None) -> RuntimeConfig:
    """Load and validate a `runtime.yaml` file into a `RuntimeConfig`.

    Reads the path from `NEWTON_MCP_RUNTIME_CONFIG` when `path` is not given
    (blank is treated as unset). Never falls back to a default config: a
    missing variable, a missing file, or unparseable YAML all raise
    `ValueError` naming the variable or path plus the underlying error.
    """
    if path is None:
        raw = os.environ.get(RUNTIME_CONFIG_ENV_VAR)
        if raw is None or not raw.strip():
            raise ValueError(
                f"{RUNTIME_CONFIG_ENV_VAR} is unset or blank; set it to the path of a runtime.yaml "
                "allow-list, or pass an explicit path to load_runtime_config()"
            )
        path = raw.strip()

    resolved = Path(path)
    if not resolved.is_file():
        raise ValueError(f"runtime config file not found: {resolved}")

    try:
        raw_text = resolved.read_text()
    except OSError as exc:
        raise ValueError(f"could not read runtime config file {resolved}: {exc}") from None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"runtime config file {resolved} is not parseable YAML: {exc}") from None

    return RuntimeConfig.model_validate(data or {})
