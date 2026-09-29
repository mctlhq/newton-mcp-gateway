"""`runtime.yaml`: the reviewable allow-list of MCP server + tool capabilities.

Every model forbids unknown keys (`extra="forbid"`) on purpose: a misspelt
key in an actuator allow-list must fail loudly at load time, never silently
widen what a physical action can reach. See docs/action-runtime.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from newton_mcp.canonical import sha256_hex

RUNTIME_CONFIG_ENV_VAR = "NEWTON_MCP_RUNTIME_CONFIG"

_DEFAULT_PORT_FOR_SCHEME = {"http": 80, "https": 443}


def _canonical_url(url: str) -> str:
    """Lowercase the scheme and the host-name part of `url`; elide a default port.

    Userinfo (`user:pass@`), IPv6 brackets, path, query and fragment are kept
    byte-exact. The netloc is rebuilt from its own substrings -- never from
    `urlsplit(...).hostname`/`.port` -- because those properties silently drop
    userinfo and IPv6 brackets, which would make two URLs that differ only in
    credentials fingerprint the same. Nothing else is normalised: two URLs
    that differ only in percent-encoding or a trailing slash fingerprint
    differently, which fails safe (a spurious invalidation, never a spurious
    validity).
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc

    userinfo = ""
    hostport = netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        userinfo += "@"

    port: str | None
    if hostport.startswith("["):
        end = hostport.index("]")
        host = hostport[: end + 1].lower()
        rest = hostport[end + 1 :]
        port = rest[1:] if rest.startswith(":") else None
    elif ":" in hostport:
        host, _, port = hostport.rpartition(":")
        host = host.lower()
    else:
        host = hostport.lower()
        port = None

    default_port = _DEFAULT_PORT_FOR_SCHEME.get(scheme)
    port_suffix = ""
    if port and (default_port is None or port != str(default_port)):
        port_suffix = f":{port}"

    canonical_netloc = f"{userinfo}{host}{port_suffix}"
    return urlunsplit((scheme, canonical_netloc, parsed.path, parsed.query, parsed.fragment))


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

    @property
    def transport_fingerprint(self) -> str:
        """sha256 of the canonical transport form; see docs/action-runtime.md.

        For `streamable-http` this is `{"kind": "streamable-http", "url":
        <canonical url>}`. For `stdio` it is `{"kind": "stdio", "command":
        ..., "args": [...], "env": {...}}` with the full `env` mapping --
        names and values. An env *value* enters only this digest: it never
        appears in `binding_identity`, an `Approval`, a reason string or a
        log. Changing `url` (http), or `command`/`args`/any `env` name or
        value (stdio), changes this fingerprint -- a stdio server's target is
        often configured through env, so a credential rotation also
        invalidates outstanding approvals, which fails safe for short-lived
        approvals.
        """
        transport = self.transport
        canonical: dict[str, Any]
        if isinstance(transport, HttpTransport):
            canonical = {"kind": "streamable-http", "url": _canonical_url(transport.url)}
        elif isinstance(transport, StdioTransport):
            canonical = {
                "kind": "stdio",
                "command": transport.command,
                "args": list(transport.args),
                "env": dict(transport.env),
            }
        else:  # pragma: no cover - the discriminated union covers every case
            raise ValueError(f"unsupported transport {transport!r}")
        return sha256_hex(canonical)

    @property
    def binding_identity(self) -> str:
        """The configured label plus a canonical transport fingerprint.

        This, not `resolved_identity` alone, is what an `Approval`'s
        `server_identity` binds to: a label-only binding would leave every
        outstanding approval valid after an operator re-points this server's
        transport to somewhere else under the same name.
        """
        return f"{self.resolved_identity}@sha256:{self.transport_fingerprint}"


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
    read_arguments: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = False

    @field_validator("goal_prefixes")
    @classmethod
    def _reject_blank_goal_prefixes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """An empty or whitespace-only prefix matches every goal via `str.startswith`.

        `goal_prefixes: [""]` would silently widen a capability to match any
        contract goal, which contradicts the fail-loudly philosophy described
        in this module's docstring. Reject it at load time instead.
        """
        for prefix in value:
            if not prefix.strip():
                raise ValueError(
                    f"goal_prefixes entries must not be empty or blank, got {prefix!r}; "
                    "an empty prefix matches every goal via str.startswith('')"
                )
        return value


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
