from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from newton_mcp.canonical import canonical_json_bytes, canonical_timestamp, sha256_hex


def test_canonical_json_bytes_sorts_keys_and_has_no_whitespace() -> None:
    encoded = canonical_json_bytes({"b": 1, "a": 2})
    assert encoded == b'{"a":2,"b":1}'


def test_canonical_json_bytes_round_trips_non_ascii_literally() -> None:
    encoded = canonical_json_bytes({"name": "café"})
    assert "café".encode("utf-8") in encoded
    assert b"\\u00e9" not in encoded


def test_canonical_json_bytes_rejects_non_string_mapping_key() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({1: "a"})


def test_canonical_json_bytes_rejects_non_string_key_nested() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"outer": [{2: "b"}]})


def test_canonical_json_bytes_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("nan")})


def test_canonical_json_bytes_rejects_infinity() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("inf")})
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("-inf")})


def test_int_and_float_of_same_value_produce_different_bytes() -> None:
    assert canonical_json_bytes({"x": 20}) != canonical_json_bytes({"x": 20.0})
    assert sha256_hex({"x": 20}) != sha256_hex({"x": 20.0})


def test_key_insertion_order_does_not_affect_bytes() -> None:
    assert canonical_json_bytes({"a": 1, "b": 2}) == canonical_json_bytes({"b": 2, "a": 1})


def test_canonical_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        canonical_timestamp(datetime(2026, 1, 1, 12, 0, 0))


def test_canonical_timestamp_renders_utc_with_microseconds() -> None:
    moment = datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=timezone.utc)
    assert canonical_timestamp(moment) == "2026-01-01T12:00:00.500000Z"


def test_canonical_timestamp_same_instant_different_offsets_renders_identically() -> None:
    utc_moment = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    offset_moment = utc_moment.astimezone(timezone(timedelta(hours=5)))
    assert canonical_timestamp(utc_moment) == canonical_timestamp(offset_moment)


def test_sha256_hex_is_deterministic() -> None:
    assert sha256_hex({"a": 1}) == sha256_hex({"a": 1})
    assert len(sha256_hex({"a": 1})) == 64
