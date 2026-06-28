"""Generic runtime helpers for validating script JSON argv."""

from __future__ import annotations

from typing import Any

_EMPTY_SENTINELS = (None, "", [], {})


def strict_json_argv_guard(payload: dict[str, Any], spec: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate JSON argv against a script-local spec and return safe args.

    The spec is authored by the script for the parameters its own core logic
    reads. This helper is intentionally business-agnostic and contains no
    platform field vocabulary.
    """
    if not isinstance(payload, dict):
        raise ValueError("argv JSON must be an object")
    if not isinstance(spec, dict):
        raise TypeError("argv spec must be a dict")

    allowed_keys = set(spec)
    required_keys = {
        key
        for key, rule in spec.items()
        if isinstance(rule, dict) and rule.get("required", True)
    }

    unknown = set(payload) - allowed_keys
    if unknown:
        raise ValueError(f"unknown argv keys: {sorted(unknown)}")

    missing = required_keys - set(payload)
    if missing:
        raise ValueError(f"missing required argv keys: {sorted(missing)}")

    validated: dict[str, Any] = {}
    for key, rule in spec.items():
        if not isinstance(rule, dict):
            raise TypeError(f"argv spec for {key} must be a dict")

        required = bool(rule.get("required", True))
        has_value = key in payload
        if not has_value:
            if "default" in rule:
                validated[key] = rule["default"]
            continue

        value = payload[key]
        if required and value in _EMPTY_SENTINELS:
            raise ValueError(f"empty required argv value: {key}")

        expected_type = rule.get("type")
        if expected_type is not None and not isinstance(value, expected_type):
            type_name = getattr(expected_type, "__name__", str(expected_type))
            raise TypeError(f"invalid argv type for {key}: expected {type_name}")

        validated[key] = value

    return validated
