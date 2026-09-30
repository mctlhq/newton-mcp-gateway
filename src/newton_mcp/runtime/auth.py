"""Resolve a declared `HttpAuth` block into an actual header at connect time.

Kept separate from `runtime/config.py` (which stays purely declarative and
computable without any environment) and imports nothing from `catalog.py` or
`executor.py`, so `default_client_factory()` in `catalog.py` can import this
module without an import cycle. See docs/action-runtime.md and design.md for
`mctlhq/newton-mcp-gateway#28`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from newton_mcp.runtime.audit import REDACTED
from newton_mcp.runtime.config import HttpAuth

#: Control characters (including DEL) a resolved header value must not
#: contain. `\t` is excluded deliberately -- httpx2/h11 accept a literal tab
#: inside a header value (RFC 9110 allows it as `obs-text`/whitespace); CR
#: and LF are what make header/request-smuggling injection possible, and the
#: rest of C0 plus DEL are rejected defensively alongside them.
_FORBIDDEN_VALUE_CHARS = frozenset(chr(c) for c in range(0x00, 0x20) if chr(c) != "\t") | {"\x7f"}


class MissingAuthSecret(Exception):
    """The environment variable an `HttpAuth` names is unset or blank, or unusable.

    Raised by `resolve_auth_header()`. The message names the *variable* and
    the *server*, never the value -- there is no value to show when the
    variable is unset or blank, and when a nonblank value is rejected for
    containing a control character or being unencodable, the value itself is
    still never echoed.
    """


def resolve_auth_header(
    auth: HttpAuth, *, server_name: str, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """Return `(header_name, header_value)` for `auth`, or raise `MissingAuthSecret`.

    Reads `auth.env` from `env` (defaults to `os.environ`) so tests can
    inject a fake mapping and never need `monkeypatch.setenv`. A blank value
    (post-`.strip()`) is treated the same as an unset one. A *nonblank*
    value is never trimmed or otherwise altered -- surrounding whitespace in
    a real credential is preserved byte-exact -- but a value containing a
    control character (CR, LF, other C0, or DEL) or one `httpx2` cannot
    encode into a header is rejected with a fixed, value-free error before
    it ever reaches an HTTP client, rather than letting `httpx2` itself
    raise (and potentially echo the value in its own exception text).

    Raises:
        MissingAuthSecret: the variable is unset, blank, or its value fails
            the safety checks above.
    """
    source = os.environ if env is None else env
    raw = source.get(auth.env)
    if raw is None or not raw.strip():
        raise MissingAuthSecret(
            f"server {server_name!r} declares auth from environment variable {auth.env!r}, "
            "which is unset or blank; set it or remove the auth block "
            "(the runtime will not connect unauthenticated)"
        )

    value = raw  # preserve the credential byte-exact; only *validated*, never altered
    if any(ch in _FORBIDDEN_VALUE_CHARS for ch in value):
        raise MissingAuthSecret(
            f"server {server_name!r}'s environment variable {auth.env!r} contains a control "
            "character, which is not a valid HTTP header value; fix the variable's value "
            "(the value is intentionally not shown)"
        )
    header_value = f"{auth.scheme} {value}" if auth.scheme else value
    try:
        header_value.encode("latin-1")
    except UnicodeEncodeError:
        raise MissingAuthSecret(
            f"server {server_name!r}'s environment variable {auth.env!r} cannot be encoded into "
            "an HTTP header value; fix the variable's value (the value is intentionally not shown)"
        ) from None

    return auth.header, header_value


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty, non-blank occurrence of a secret in `text` with `REDACTED`.

    Defence in depth for a diagnostic path that, against the design here,
    ends up formatting text that might contain a resolved header value --
    every *intended* diagnostic path stays class-only or value-free on its
    own and should never need this. A no-op for any secret that is empty or
    blank after stripping: redacting a blank string would otherwise replace
    every position in `text` (or turn whitespace-only text into noise),
    which is worse than doing nothing.
    """
    result = text
    for secret in secrets:
        if not secret or not secret.strip():
            continue
        result = result.replace(secret, REDACTED)
    return result
