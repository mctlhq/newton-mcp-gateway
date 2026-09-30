"""`runtime.yaml`: the reviewable allow-list of MCP server + tool capabilities.

Every model forbids unknown keys (`extra="forbid"`) on purpose: a misspelt
key in an actuator allow-list must fail loudly at load time, never silently
widen what a physical action can reach. See docs/action-runtime.md.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from newton_mcp.canonical import sha256_hex

RUNTIME_CONFIG_ENV_VAR = "NEWTON_MCP_RUNTIME_CONFIG"

_DEFAULT_PORT_FOR_SCHEME = {"http": 80, "https": 443}

#: `auth` may carry exactly these keys. Anything else -- in particular a
#: literal secret under `value`/`token`/`secret`/`password` -- is rejected by
#: `_validate_auth_mapping()` before Pydantic's own machinery can build a
#: `ValidationError` that would echo it back in `input_value=...`.
_ALLOWED_AUTH_KEYS = frozenset({"header", "scheme", "env"})

#: POSIX environment variable name: `^[A-Za-z_][A-Za-z0-9_]*$`.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: RFC 9110 `token` characters -- what a header field-name and a single auth
#: scheme are both required to be. In particular this excludes CR, LF, colon
#: and space, which rules out header injection via a crafted `header`/`scheme`.
_HTTP_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

#: Headers the MCP streamable-http transport manages itself; a declared `auth`
#: header naming one of these (case-insensitively) would collide with it.
_SDK_MANAGED_HEADERS = frozenset({"content-type", "accept", "mcp-session-id", "mcp-protocol-version"})


class AuthConfigError(Exception):
    """Raised for an invalid `transport.auth` block. Deliberately NOT a `ValueError`.

    pydantic-core converts a `ValueError`/`AssertionError` raised inside a
    validator into a `ValidationError` whose rendered message embeds
    `input_value=<the raw input>` -- which would be the secret itself when an
    operator writes `auth: {value: "sk-live-..."}`. Any other exception type
    propagates out of `model_validate()` untouched, so this one carries only
    fixed field names and, at most, a validated env-var/header name or a
    validated server name -- never a rejected key or value.
    """


def _validate_auth_mapping(auth: Any, *, server_name: str | None = None) -> None:
    """Screen a raw `auth` mapping. Raises `AuthConfigError`; never echoes a value.

    Called from three raw-input screens (`ServerConfig`, `HttpTransport`, and
    indirectly for the stdio-rejection case) so that config loaded through
    `RuntimeConfig`, a directly validated `HttpTransport`, and a directly
    validated `ServerConfig` are all safe -- never only the file-loading path.
    Idempotent and side-effect free: calling it more than once on the same
    input is harmless, which is what makes the ServerConfig-level screen and
    the transport-level screen able to both run without conflict.
    """
    context = f" (server {server_name!r})" if server_name else ""
    if not isinstance(auth, dict):
        raise AuthConfigError(
            f"transport.auth must be a mapping with keys header/scheme/env{context}; "
            "the rejected value is intentionally not shown"
        )
    extra = set(auth) - _ALLOWED_AUTH_KEYS
    if extra:
        raise AuthConfigError(
            f"transport.auth may only contain {sorted(_ALLOWED_AUTH_KEYS)}{context}; an unsupported "
            "auth field was given. A secret value must never appear in runtime.yaml -- name an "
            "environment variable with `env:` instead. (The rejected key and its value are "
            "intentionally not shown.)"
        )

    header = auth.get("header")
    if not isinstance(header, str) or not _HTTP_TOKEN_RE.match(header):
        raise AuthConfigError(
            f"transport.auth.header must be a valid HTTP field-name token (RFC 9110){context}; "
            "the rejected value is intentionally not shown"
        )
    if header.casefold() in _SDK_MANAGED_HEADERS:
        raise AuthConfigError(
            f"transport.auth.header must not name an SDK-managed header "
            f"({sorted(_SDK_MANAGED_HEADERS)}){context}; got {header.casefold()!r}"
        )

    scheme = auth.get("scheme")
    if scheme is not None and (not isinstance(scheme, str) or not _HTTP_TOKEN_RE.match(scheme)):
        raise AuthConfigError(
            f"transport.auth.scheme, when present, must be a single HTTP token with no "
            f"whitespace{context}; the rejected value is intentionally not shown"
        )

    env = auth.get("env")
    if not isinstance(env, str) or not _ENV_NAME_RE.match(env):
        raise AuthConfigError(
            f"transport.auth.env must match ^[A-Za-z_][A-Za-z0-9_]*$ (a POSIX environment "
            f"variable name){context}; the rejected value is intentionally not shown"
        )


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

    @model_validator(mode="before")
    @classmethod
    def _reject_auth_block(cls, data: Any) -> Any:
        """Reject a stdio `auth` block safely, before `extra="forbid"` can echo it.

        `StdioTransport` declares no `auth` field, so `extra="forbid"`
        already rejects one -- but its `ValidationError` would embed the
        rejected mapping verbatim in `input_value=...`, which is the secret
        itself when the block carries one. This runs first (a `mode="before"`
        validator always runs ahead of the model's own extra-field check) and
        raises a value-free `AuthConfigError` instead. Guards direct
        `StdioTransport.model_validate(...)` too, not just the
        `RuntimeConfig`/`ServerConfig` load path.
        """
        if isinstance(data, dict) and "auth" in data and data["auth"] is not None:
            raise AuthConfigError(
                "stdio transports do not support an auth block; stdio already passes "
                "credentials through `env`"
            )
        return data


class HttpAuth(BaseModel):
    """A declared auth header for a `streamable-http` server.

    Only `header`, `scheme` and `env` -- the header *name*, an optional
    scheme prefix, and the *name* of the environment variable the value
    comes from. The value itself is never part of this model, never loaded
    from `runtime.yaml`, and is resolved only at connect time by
    `newton_mcp.runtime.auth.resolve_auth_header()`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    header: str
    scheme: str | None = None
    env: str


class HttpTransport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["streamable-http"]
    url: str
    auth: HttpAuth | None = None

    @model_validator(mode="before")
    @classmethod
    def _screen_auth_block(cls, data: Any) -> Any:
        """Screen a raw `auth` mapping before ordinary Pydantic validation.

        Runs ahead of `HttpAuth`'s own field validation (a `mode="before"`
        validator always does) so an invalid block raises `AuthConfigError`
        -- never a `ValidationError` that would echo the rejected value.
        `auth: null` is treated as omitted. This is a safety net for a
        directly validated `HttpTransport`; `ServerConfig._screen_transport_auth`
        below runs the same check earlier still, before the discriminated
        union even picks a transport kind.
        """
        if not isinstance(data, dict) or "auth" not in data:
            return data
        auth = data["auth"]
        if auth is None:
            return data
        _validate_auth_mapping(auth)
        return data


Transport = Annotated[StdioTransport | HttpTransport, Field(discriminator="kind")]


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Transport
    identity: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _screen_transport_auth(cls, data: Any) -> Any:
        """Runtime-level raw-input screen: catches what the per-transport screens cannot.

        `Transport`'s discriminated union picks a member by reading
        `transport.kind` *before* attempting to construct either member
        model -- so when `kind` is missing, misspelled, or `"stdio"`, neither
        `HttpTransport._screen_auth_block` nor `StdioTransport._reject_auth_block`
        is ever reached, and Pydantic's own union-tag-mismatch error (or
        `StdioTransport`'s `extra="forbid"`) would embed the whole raw
        `transport` mapping -- auth block included -- in `input_value=...`.
        Running this ahead of any nested field validation closes that gap for
        every kind, known or not; only `"streamable-http"` is allowed to
        proceed to `HttpTransport`'s own (now redundant, but still safe)
        screen. `auth: null` is treated as omitted, same as everywhere else.
        """
        if not isinstance(data, dict):
            return data
        transport = data.get("transport")
        if not isinstance(transport, dict) or "auth" not in transport:
            return data
        auth = transport["auth"]
        if auth is None:
            return data
        name = data.get("name")
        server_name = name if isinstance(name, str) else None
        if transport.get("kind") != "streamable-http":
            suffix = f" (server {server_name!r})" if server_name else ""
            raise AuthConfigError(f"transport.auth is only supported for a streamable-http transport{suffix}")
        _validate_auth_mapping(auth, server_name=server_name)
        return data

    @property
    def resolved_identity(self) -> str:
        """The identity `CandidateAction.server_identity` names; defaults to `name`."""
        return self.identity if self.identity is not None else self.name

    @property
    def transport_fingerprint(self) -> str:
        """sha256 of the canonical transport form; see docs/action-runtime.md.

        For `streamable-http` this is `{"kind": "streamable-http", "url":
        <canonical url>}`, plus an `"auth": {"header": ..., "scheme": ...,
        "env": ...}` entry -- header name, scheme and env-var *name* only --
        **when and only when** `transport.auth is not None`; a server with no
        `auth` produces the exact two-key dict this property always has, so
        every existing approval stays valid. The named environment variable
        is never read here: rotating its *value* leaves this fingerprint (and
        so `binding_identity`) unchanged, while changing the `env` name, the
        `header`, or the `scheme` changes it, invalidating any approval
        issued before the change. For `stdio` it is `{"kind": "stdio",
        "command": ..., "args": [...], "env": {...}}` with the full `env`
        mapping -- names and values. An env *value* enters only this digest:
        it never appears in `binding_identity`, an `Approval`, a reason
        string or a log. Changing `url`/`auth` (http), or
        `command`/`args`/any `env` name or value (stdio), changes this
        fingerprint -- a stdio server's target is often configured through
        env, so a credential rotation also invalidates outstanding approvals,
        which fails safe for short-lived approvals.
        """
        transport = self.transport
        canonical: dict[str, Any]
        if isinstance(transport, HttpTransport):
            canonical = {"kind": "streamable-http", "url": _canonical_url(transport.url)}
            if transport.auth is not None:
                canonical["auth"] = {
                    "header": transport.auth.header,
                    "scheme": transport.auth.scheme,
                    "env": transport.auth.env,
                }
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
