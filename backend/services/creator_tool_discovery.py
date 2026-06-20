"""Config-driven discovery adapter for Creator tool manifests.

Creator owns discovery/adaptation only: it reads existing platform registries or
runtime tool modules and returns manifest dictionaries that the Creator registry
can convert into ToolCapability/ToolFunctionManifest records. It does not
implement runtime tools and it does not select tools by business keywords.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "creator_tool_discovery.json"
REQUIRED_TOOL_FIELDS = {
    "tool_id",
    "description",
    "input_schema",
    "output_schema",
    "artifact_outputs",
    "side_effects",
    "dependencies",
}
REQUIRED_FUNCTION_FIELDS = {"import_path", "function_name", "input_schema", "output_schema"}


def load_creator_tool_discovery_config(config_path: str | Path | None = None) -> dict[str, Any]:
    raw_path = config_path or os.getenv("CREATOR_TOOL_DISCOVERY_CONFIG") or DEFAULT_CONFIG_PATH
    path = _resolve_project_path(raw_path)
    if not path.is_file():
        return {"registries": [], "modules": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"registries": [], "modules": []}
    if not isinstance(data, dict):
        return {"registries": [], "modules": []}
    return {
        "registries": [str(item) for item in data.get("registries") or [] if str(item).strip()],
        "modules": [str(item) for item in data.get("modules") or [] if str(item).strip()],
    }


def discover_creator_tool_records(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return complete callable tool manifest records from configured sources."""
    config = config or load_creator_tool_discovery_config()
    records: list[dict[str, Any]] = []
    for registry_path in config.get("registries") or []:
        records.extend(_records_from_registry_path(registry_path))
    for module_name in config.get("modules") or []:
        record = _record_from_module(module_name)
        if record is not None:
            records.append(record)
    return _dedupe_records([record for record in records if is_complete_callable_tool_record(record)])


def is_complete_callable_tool_record(record: dict[str, Any]) -> bool:
    normalized = normalize_tool_record(record)
    if not REQUIRED_TOOL_FIELDS.issubset(normalized.keys()):
        return False
    if not isinstance(normalized.get("input_schema"), dict) or not normalized.get("input_schema"):
        return False
    if not isinstance(normalized.get("output_schema"), dict) or not normalized.get("output_schema"):
        return False
    if not isinstance(normalized.get("artifact_outputs"), list):
        return False
    if not isinstance(normalized.get("side_effects"), list):
        return False
    functions = normalized.get("functions")
    if not isinstance(functions, list) or not functions:
        return False
    for fn in functions:
        if not isinstance(fn, dict):
            continue
        if REQUIRED_FUNCTION_FIELDS.issubset(fn.keys()) and fn.get("import_path") and fn.get("function_name"):
            return True
    return False


def normalize_tool_record(record: dict[str, Any]) -> dict[str, Any]:
    data = dict(record or {})
    name = str(data.get("name") or data.get("tool_name") or data.get("tool_id") or "").strip()
    if name:
        data.setdefault("tool_id", name)
        data.setdefault("name", name)
    data.setdefault("description", str(data.get("display_name") or data.get("short_description") or name))
    data.setdefault("display_name", data.get("description") or name)
    data.setdefault("dependencies", [])
    data.setdefault("artifact_outputs", [])
    data.setdefault("side_effects", [])
    if "generates_file" in data and data.get("generates_file") and not data.get("side_effects"):
        data["side_effects"] = ["write_output_file"]
    if not isinstance(data.get("functions"), list):
        data["functions"] = []
    normalized_functions: list[dict[str, Any]] = []
    for raw_fn in data.get("functions") or []:
        if not isinstance(raw_fn, dict):
            continue
        fn = dict(raw_fn)
        fn.setdefault("input_schema", data.get("input_schema") or {})
        fn.setdefault("output_schema", data.get("output_schema") or {})
        fn.setdefault("artifact_outputs", data.get("artifact_outputs") or [])
        fn.setdefault("side_effects", data.get("side_effects") or [])
        normalized_functions.append(fn)
    data["functions"] = normalized_functions
    return data


def _records_from_registry_path(raw_path: str | Path) -> list[dict[str, Any]]:
    path = _resolve_project_path(raw_path)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    records = payload.get("tools") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return []
    return [normalize_tool_record(item) for item in records if isinstance(item, dict)]


def _record_from_module(module_name: str) -> dict[str, Any] | None:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    manifest = getattr(module, "MANIFEST_DATA", None)
    if manifest is None and callable(getattr(module, "manifest", None)):
        try:
            manifest = module.manifest()
        except Exception:
            manifest = None
    if isinstance(manifest, dict):
        return normalize_tool_record(manifest)

    # Initial adaptation for modules without a whole-module manifest. Signature
    # and docstring are descriptive; schema shape is adapted from type
    # annotations when present and never from function names/business keywords.
    functions: list[dict[str, Any]] = []
    for _, obj in inspect.getmembers(module, inspect.isfunction):
        raw = getattr(obj, "__creator_tool_manifest__", None)
        if isinstance(raw, dict):
            fn = dict(raw)
        else:
            fn = _manifest_from_signature(module.__name__, obj)
        if not isinstance(fn, dict):
            continue
        fn.setdefault("function_name", obj.__name__)
        fn.setdefault("import_path", module.__name__)
        fn.setdefault("signature", str(inspect.signature(obj)))
        fn.setdefault("short_description", inspect.getdoc(obj) or obj.__name__)
        functions.append(fn)
    if not functions:
        return None
    return normalize_tool_record({
        "name": module.__name__.split(".")[-1],
        "tool_id": module.__name__.split(".")[-1],
        "description": inspect.getdoc(module) or module.__name__,
        "functions": functions,
        "input_schema": functions[0].get("input_schema") or {},
        "output_schema": functions[0].get("output_schema") or {},
        "artifact_outputs": functions[0].get("artifact_outputs") or [],
        "side_effects": functions[0].get("side_effects") or [],
        "dependencies": functions[0].get("dependencies") or [],
    })


def _manifest_from_signature(module_name: str, func: Any) -> dict[str, Any] | None:
    if getattr(func, "__name__", "").startswith("_"):
        return None
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        properties[name] = _schema_from_annotation(param.annotation)
        if param.default is inspect.Signature.empty:
            required.append(name)
    if not properties:
        return None
    return_schema = _schema_from_annotation(signature.return_annotation)
    if not return_schema:
        return None
    return {
        "function_name": func.__name__,
        "import_path": module_name,
        "input_schema": {"type": "object", "properties": properties, "required": required},
        "output_schema": return_schema,
        "artifact_outputs": [],
        "side_effects": [],
        "dependencies": [],
        "when_to_use": inspect.getdoc(func) or "",
        "short_description": (inspect.getdoc(func) or func.__name__).splitlines()[0],
    }


def _schema_from_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {"type": "any"}
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if annotation in {str, "str"}:
        return {"type": "string"}
    if annotation in {int, "int"}:
        return {"type": "integer"}
    if annotation in {float, "float"}:
        return {"type": "number"}
    if annotation in {bool, "bool"}:
        return {"type": "boolean"}
    if annotation in {dict, "dict"} or origin is dict:
        return {"type": "object", "additionalProperties": True}
    if annotation in {list, "list", tuple, "tuple", set, "set"} or origin in {list, tuple, set}:
        item_schema = _schema_from_annotation(args[0]) if args else {"type": "any"}
        return {"type": "array", "items": item_schema}
    return {"type": "any"}


def _resolve_project_path(raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = str(record.get("name") or record.get("tool_id") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out
