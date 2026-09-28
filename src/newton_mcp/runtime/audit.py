"""Append-only audit trail for action-lifecycle transitions.

`AuditSink` is a `Protocol` (matching `SupportsListTools` in
`runtime/catalog.py`), so the sink is a seam a caller can substitute, not a
class hierarchy. `AuditEvent` is a frozen Pydantic model whose `from`/`to`
JSON keys are Python keywords, so the model fields are `from_state`/
`to_state` with `alias="from"`/`alias="to"` and `populate_by_name=True`;
serialize with `model_dump(by_alias=True, exclude_none=True)` to get the
issue's field names on the wire.

This module imports nothing from `runtime/lifecycle.py` or `action/`:
`from_state`/`to_state` are typed `str`, not `ActionState`, which keeps this
module reusable by a future executor (#8) through the same sink without an
import cycle back into the state machine. See docs/action-runtime.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

AUDIT_PATH_ENV_VAR = "NEWTON_MCP_AUDIT_PATH"

#: Case-insensitive substring match against an argument key name. Deliberately
#: over-eager: a benign key like `keypad_zone` is redacted too because it
#: contains "key". There is NO value-shape detection anywhere in this module --
#: a secret passed under a harmless key name (e.g. `note`) is not caught, and
#: `docs/action-runtime.md` states that limitation outright.
SECRET_KEY_PATTERNS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "auth",
    "bearer",
    "cookie",
    "session",
    "signature",
)
REDACTED = "[redacted]"
_MAX_VALUE_CHARS = 500


def _truncate(text: str) -> str:
    """Truncate `text` to `_MAX_VALUE_CHARS`, following `runtime/catalog.py`'s `_truncate` convention."""
    if len(text) <= _MAX_VALUE_CHARS:
        return text
    return text[: _MAX_VALUE_CHARS - 3] + "..."


def _is_secret_key(key: str) -> bool:
    lowered = key.casefold()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


def _redact_value(value: Any, *, key: str | None) -> Any:
    if key is not None and _is_secret_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {inner_key: _redact_value(inner_value, key=inner_key) for inner_key, inner_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, key=None) for item in value]
    if isinstance(value, str):
        return _truncate(value)
    return value


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a new structure with any secret-looking value replaced by `REDACTED`.

    Recurses into nested mappings and lists. A key is treated as secret when
    its casefolded name contains any of `SECRET_KEY_PATTERNS` as a substring.
    Surviving string values longer than `_MAX_VALUE_CHARS` are truncated.
    Never mutates `args` -- the executor's own `args` for the actual tool
    call must stay intact.
    """
    return _redact_value(args, key=None)


class AuditSink(Protocol):
    """The seam `transition()` writes through. A `Protocol`, not a base class."""

    def write(self, event: "AuditEvent") -> None: ...


class AuditEvent(BaseModel):
    """One line of the audit log: a single accepted lifecycle transition.

    `from_state`/`to_state` are typed `str` rather than `newton_mcp.runtime.lifecycle.ActionState`
    (an `ActionState` is a `StrEnum`, so passing one here is type-correct and
    serializes to its value). `args`/`args_digest` are populated only when
    the caller supplied `args` to `transition()`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    observation_id: str
    action_id: str
    tool_call_id: str
    verification_id: str
    from_state: str = Field(alias="from")
    to_state: str = Field(alias="to")
    reason: str
    attempt: int
    verified_failure: bool
    at: str
    args: dict[str, Any] | None = None
    args_digest: str | None = None


class MemoryAuditSink:
    """In-memory sink: the default (auditing disabled) mode, and what tests use.

    `events` collects every written `AuditEvent` in the order `write()` was
    called. No file is ever touched.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonlAuditSink:
    """Append-only JSONL sink: one compact UTF-8 JSON line per event.

    Each `write()` opens `path` in append mode (`open(path, "a")`), writes
    exactly one `model_dump(by_alias=True, exclude_none=True)` line
    terminated by a single `\\n`, and closes -- re-opening an existing file
    never truncates it, so previously written lines survive a process
    restart. Opening per write keeps the sink stateless and the file
    consistent without an explicit flush/fsync dance.

    This is a small, single-process sink, deliberately: no log rotation, no
    retention policy, no fsync-per-line durability guarantee, and no
    multi-process write coordination. The volume this module produces is one
    line per state change of a physical action, not a hot path, so none of
    that is needed for the scope of this proposal.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: AuditEvent) -> None:
        line = json.dumps(
            event.model_dump(by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def load_audit_sink(path: str | Path | None = None) -> AuditSink:
    """Build an `AuditSink` from `NEWTON_MCP_AUDIT_PATH`, or an explicit `path`.

    This differs from `load_runtime_config()` / `load_policy()` in its unset
    behaviour, deliberately: those two fail loudly when their variable is
    unset, because an allow-list that silently widens is a safety hole. An
    audit sink is not an authority boundary -- it is opt-in observability --
    and an operator who never set `NEWTON_MCP_AUDIT_PATH` never asked for a
    file, so an unset or blank value returns an in-memory `MemoryAuditSink`
    (the effectively-disabled default the test suite relies on).

    A value that *is* set but unusable is a different case and still fails
    loudly: a path naming an existing directory, or one whose parent
    directory does not exist, or one that is not writable, raises
    `ValueError` naming `NEWTON_MCP_AUDIT_PATH` and the offending path rather
    than silently degrading to the in-memory sink -- a typo in the path must
    be loud, the same fail-loudly instinct as the allow-list loaders.
    """
    if path is None:
        raw = os.environ.get(AUDIT_PATH_ENV_VAR)
        if raw is None or not raw.strip():
            return MemoryAuditSink()
        candidate: str | Path = raw.strip()
    else:
        candidate = path

    resolved = Path(candidate)

    if resolved.is_dir():
        raise ValueError(f"{AUDIT_PATH_ENV_VAR} names a directory, not a file: {resolved}")

    parent = resolved.parent
    if not parent.is_dir():
        raise ValueError(
            f"{AUDIT_PATH_ENV_VAR} points at {resolved}, whose parent directory {parent} does not exist"
        )

    write_target = resolved if resolved.exists() else parent
    if not os.access(write_target, os.W_OK):
        raise ValueError(f"{AUDIT_PATH_ENV_VAR} points at {resolved}, which is not writable")

    return JsonlAuditSink(resolved)
