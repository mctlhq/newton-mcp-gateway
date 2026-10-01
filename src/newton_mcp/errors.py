"""SDK-free error types shared by the action library and the MCP server."""

from __future__ import annotations


class InputValidationError(ValueError):
    """A deliberate rejection of caller input, with a safe, actionable message.

    Subclasses `ValueError` so existing callers keep working. The MCP server
    translates only this type into a client-visible tool error; any other
    exception (including a backend `ValueError`) stays a generic failure.
    """
