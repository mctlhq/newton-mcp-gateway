"""Canonical JSON and timestamp encoding for digest-stable payloads.

Both `action/` (approval binding) and `runtime/` (transport fingerprint) need
to hash a plain Python value to the same bytes regardless of dict insertion
order. This module sits at `src/newton_mcp/`, outside either package, so
neither has to import the other to share it -- `action/` must never import
`runtime/` (see docs/action-runtime.md), and this module depends on neither.

No numeric or string normalisation is performed here, deliberately: `20` and
`20.0` serialise to different bytes, and string case or percent-encoding
inside a value is left untouched. A digest is only useful if it changes
whenever the input an operator cares about changes; normalising anything on
the way in risks two different actions collapsing into one digest, which is
the one failure mode this module must never produce. An ambiguous input (a
non-string mapping key, `NaN`, `Infinity`, a naive `datetime`) is refused
outright rather than coerced or dropped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def _reject_non_string_keys(value: Any) -> Any:
    """Walk `value`, raising on any non-string mapping key found anywhere inside it.

    `json.dumps` would otherwise silently coerce a non-string key (e.g. the
    int ``1``) to its string form, making ``{1: "a"}`` and ``{"1": "a"}``
    digest identically -- exactly the collision this module must refuse
    rather than produce. Returns a walked copy with tuples turned into lists,
    which does not change the resulting JSON (a JSON array either way).
    """
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise ValueError(f"canonical_json_bytes: non-string mapping key {key!r} is not allowed")
        return {key: _reject_non_string_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reject_non_string_keys(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Encode `value` as canonical UTF-8 JSON: sorted keys, no whitespace, literal non-ASCII.

    Raises:
        ValueError: `value` contains a non-string mapping key, `NaN`,
            `Infinity`, `-Infinity`, or any type `json.dumps` cannot encode.
    """
    walked = _reject_non_string_keys(value)
    try:
        text = json.dumps(
            walked,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"canonical_json_bytes: value is not canonically encodable: {exc}") from exc
    return text.encode("utf-8")


def sha256_hex(value: Any) -> str:
    """sha256 hex digest of `canonical_json_bytes(value)`."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_timestamp(moment: datetime) -> str:
    """Render an aware `datetime` as UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

    Raises:
        ValueError: `moment` is naive (no usable `tzinfo`) -- a naive
            timestamp would silently mean "whatever the caller's local zone
            happens to be", which must never enter a security-relevant
            binding payload.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("canonical_timestamp: moment must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
