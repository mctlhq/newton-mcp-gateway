"""Embedded copy of `examples/physical-action.json`.

The wheel ships only `src/newton_mcp` (`[tool.hatch.build.targets.wheel]` in
`pyproject.toml`); `examples/` is not included in the built package, so the
mock backend cannot read the file from disk at runtime. This module embeds
the file's contents as a literal instead. `tests/test_action_contract.py`
enforces that the literal stays byte-equivalent in value to the JSON file,
the same convention `test_schema_file_is_in_sync_with_model` already
established for `schemas/physical-action-contract.schema.json`.
"""

from __future__ import annotations

from typing import Any

MOCK_CONTRACT_EXAMPLE: dict[str, Any] = {
    "version": "0.2",
    "goal": "reduce_room_temperature",
    "reason": "The occupied kitchen reached 29.4 C while the previous window was unoccupied.",
    "confidence": 0.96,
    "target": {"type": "environment", "location": "kitchen"},
    "constraints": {
        "desired_temperature_c": 23,
        "minimum_temperature_c": 20,
        "maximum_temperature_c": 25,
    },
    "risk": "low",
    "reversible": True,
    "verification": {
        "condition": {"path": "temperature_c", "op": "le", "value": 24},
        "timeout_seconds": 600,
        "retry_limit": 1,
    },
    "evidence": {
        "observation_id": "obs-2026-09-28-0001",
        "summary": "occupancy=true, temperature_c=29.4",
    },
}
