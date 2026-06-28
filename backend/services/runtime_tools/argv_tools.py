"""Generic runtime helpers for validating script JSON argv."""

from __future__ import annotations

from typing import Any

_TYPE_ALIASES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "array": list,
    "list": list,
    "object": dict,
    "dict": dict,
    "number": (int, float),
    "boolean": bool,
    "bool": bool,
}


def _is_empty_required_value(value: Any) -> bool:
    return (
        value is None
        or value == ""
        or (isinstance(value, list) and len(value) == 0)
        or (isinstance(value, dict) and len(value) == 0)
    )


def _resolve_expected_type(key: str, expected_type: Any) -> type | tuple[type, ...] | None:
    if expected_type is None:
        return None
    if isinstance(expected_type, str):
        resolved = _TYPE_ALIASES.get(expected_type.strip().lower())
        if resolved is None:
            raise TypeError(f"unknown argv type alias for {key}: {expected_type}")
        return resolved
    if isinstance(expected_type, type):
        return expected_type
    if isinstance(expected_type, tuple) and all(isinstance(item, type) for item in expected_type):
        return expected_type
    raise TypeError(f"argv spec type for {key} must be a type or supported type alias")


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
        if required and _is_empty_required_value(value):
            raise ValueError(f"empty required argv value: {key}")

        expected_type = _resolve_expected_type(key, rule.get("type"))
        if expected_type is not None and not isinstance(value, expected_type):
            if isinstance(expected_type, tuple):
                type_name = " or ".join(getattr(item, "__name__", str(item)) for item in expected_type)
            else:
                type_name = getattr(expected_type, "__name__", str(expected_type))
            raise TypeError(f"invalid argv type for {key}: expected {type_name}")

        validated[key] = value

    return validated
