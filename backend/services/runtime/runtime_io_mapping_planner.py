"""Model-planned, execution-scoped adaptation between real runtime IO ports.

This module deliberately does not infer an operation from a Python value.  A
model selects one of the executor's declared capabilities from source and
target schemas; the runtime merely validates and performs that selection.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping


DEFAULT_RUNTIME_IO_CAPABILITIES: dict[str, list[dict[str, str]]] = {
    "operations": [
        {"name": "serialize_json_file", "input": "object", "output": "file_outputs"},
        {"name": "json_stringify", "input": "object", "output": "text"},
        {"name": "direct_pass", "input": "array[string]", "output": "file_outputs"},
    ]
}


class RuntimeIOMappingError(RuntimeError):
    """A mapping could not be planned or was outside runtime capabilities."""

    code = "runtime_io_mapping_failure"


@dataclass(frozen=True)
class RuntimeExecutionIOMapping:
    """An ephemeral mapping for exactly one execution."""

    source: dict[str, str]
    target: dict[str, str]
    operation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ModelPlanner = Callable[[list[dict[str, str]]], str | Mapping[str, Any] | Awaitable[str | Mapping[str, Any]]]


def _schema_port_type(schema: Mapping[str, Any], *, target: bool = False) -> str:
    """Project explicit schema metadata to a capability port type (not a choice)."""
    if target and schema.get("name"):
        return str(schema["name"])
    schema_type = str(schema.get("type") or "")
    if schema_type == "array" and isinstance(schema.get("items"), Mapping):
        return f"array[{schema['items'].get('type') or 'unknown'}]"
    return schema_type


def _json_object(value: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeIOMappingError("runtime IO planner returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeIOMappingError("runtime IO planner response must be an object")
    return parsed


class RuntimeIOMappingPlanner:
    """Ask a model for a capability-bounded runtime mapping."""

    def __init__(self, model_call: ModelPlanner):
        if not callable(model_call):
            raise TypeError("runtime IO mapping requires a model_call")
        self._model_call = model_call

    async def plan(
        self,
        *,
        frozen_interface_contract: Mapping[str, Any],
        function_output_schema: Mapping[str, Any],
        actual_runtime_output: Any,
        platform_output_contract: Mapping[str, Any],
        runtime_capabilities: Mapping[str, Any],
        source_member: str,
        source_output: str,
        target_sink: str,
    ) -> RuntimeExecutionIOMapping:
        operations = runtime_capabilities.get("operations")
        if not isinstance(operations, list) or not operations:
            raise RuntimeIOMappingError("runtime has no declared IO operations")
        request = {
            "frozen_interface_contract": dict(frozen_interface_contract),
            "source_schema": dict(function_output_schema),
            "actual_runtime_output": actual_runtime_output,
            "target_schema": dict(platform_output_contract),
            "runtime_capabilities": {"operations": operations},
            "required_response": {
                "source": {"member": source_member, "output": source_output},
                "target": {"sink": target_sink},
                "operation": "one declared operation name",
            },
        }
        messages = [
            {"role": "system", "content": (
                "Plan a mapping for this execution only. Select exactly one declared runtime "
                "operation using the source schema, target schema, and capabilities. Do not "
                "change or reinterpret the frozen interface contract. Return JSON only."
            )},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)},
        ]
        raw = self._model_call(messages)
        if inspect.isawaitable(raw):
            raw = await raw
        planned = _json_object(raw)
        source = planned.get("source")
        target = planned.get("target")
        operation = str(planned.get("operation") or "").strip()
        expected_source = {"member": source_member, "output": source_output}
        expected_target = {"sink": target_sink}
        if source != expected_source or target != expected_target:
            raise RuntimeIOMappingError("model changed the execution IO endpoints")
        declared = {str(item.get("name")): item for item in operations if isinstance(item, Mapping)}
        if operation not in declared:
            raise RuntimeIOMappingError(f"undeclared runtime IO operation: {operation or '<empty>'}")
        capability = declared[operation]
        source_type = _schema_port_type(function_output_schema)
        target_type = _schema_port_type(platform_output_contract, target=True)
        if capability.get("input") != source_type or capability.get("output") != target_type:
            raise RuntimeIOMappingError(
                f"operation {operation} does not connect {source_type} to {target_type}"
            )
        return RuntimeExecutionIOMapping(expected_source, expected_target, operation)


def save_runtime_io_mapping(mapping: RuntimeExecutionIOMapping, session_dir: Path) -> Path:
    """Persist mapping in the execution session, never in the Skill tree."""
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "runtime_io_mapping.json"
    path.write_text(json.dumps(mapping.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def execute_runtime_io_mapping(
    mapping: RuntimeExecutionIOMapping,
    value: Any,
    *,
    session_dir: Path,
    runtime_capabilities: Mapping[str, Any],
) -> Any:
    """Execute only the operation already selected and declared to the model."""
    operations = runtime_capabilities.get("operations")
    declared = {str(item.get("name")) for item in operations or [] if isinstance(item, Mapping)}
    if mapping.operation not in declared:
        raise RuntimeIOMappingError(f"operation is not executable in this runtime: {mapping.operation}")
    save_runtime_io_mapping(mapping, session_dir)
    if mapping.operation == "serialize_json_file":
        output_dir = session_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{mapping.source['output']}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return [str(path)]
    if mapping.operation == "json_stringify":
        return json.dumps(value, ensure_ascii=False)
    if mapping.operation == "direct_pass":
        return value
    # Capability declarations and installed adapters are independently checked:
    # declaring an unknown operation must not grant code execution authority.
    raise RuntimeIOMappingError(f"runtime adapter is not installed: {mapping.operation}")


async def adapt_runtime_output_before_validation(
    *,
    planner: RuntimeIOMappingPlanner,
    value: Any,
    session_dir: Path,
    validator: Callable[[Any], Any],
    frozen_interface_contract: Mapping[str, Any],
    function_output_schema: Mapping[str, Any],
    platform_output_contract: Mapping[str, Any],
    runtime_capabilities: Mapping[str, Any],
    source_member: str,
    source_output: str,
    target_sink: str,
) -> Any:
    """Run the canonical stdout -> planner -> adapter -> validator pipeline."""
    mapping = await planner.plan(
        frozen_interface_contract=frozen_interface_contract,
        function_output_schema=function_output_schema,
        actual_runtime_output=value,
        platform_output_contract=platform_output_contract,
        runtime_capabilities=runtime_capabilities,
        source_member=source_member,
        source_output=source_output,
        target_sink=target_sink,
    )
    adapted = execute_runtime_io_mapping(
        mapping, value, session_dir=session_dir, runtime_capabilities=runtime_capabilities
    )
    validated = validator(adapted)
    if inspect.isawaitable(validated):
        await validated
    return adapted
