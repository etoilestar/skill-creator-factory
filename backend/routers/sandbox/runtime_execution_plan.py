"""Sandbox-only runtime execution plan validation and binding resolution."""

from __future__ import annotations

from typing import Any


VERSION = "sandbox-runtime-plan/v1"
SOURCE_TYPES = {
    "envelope", "user_input", "derived_from_user_input", "default",
    "step_output", "resource",
}
_ENVELOPE_KEYS = {
    "user_request", "input", "text", "payload", "fields", "options",
    "input_files", "files", "resources",
}


def _entries_by_script(action_schema: dict) -> dict[str, dict]:
    return {
        str(entry.get("script_path") or ""): entry
        for entry in action_schema.get("entries") or []
        if isinstance(entry, dict) and entry.get("script_path")
    }


def _declared_parameters(entry: dict) -> set[str]:
    return {
        str(value) for value in (
            list(entry.get("inputs") or [])
            + list(entry.get("optional_inputs") or [])
            + list(entry.get("command_keys") or [])
        ) if value
    }


def _required_parameters(entry: dict) -> set[str]:
    return set(entry.get("inputs") or []) - set(entry.get("optional_inputs") or [])


def _binding_reference(binding: dict) -> tuple[str, str]:
    return str(binding.get("step_id") or ""), str(binding.get("output") or "")


def validate_runtime_execution_plan(plan: dict, action_schema: dict, input_envelope: dict | None = None,
                                    resource_catalog: list[dict] | dict | None = None) -> dict:
    """Validate only schema/dataflow boundaries; never infer business meaning."""
    if not isinstance(plan, dict):
        raise ValueError("runtime execution plan must be a JSON object")
    entries = _entries_by_script(action_schema)
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("runtime execution plan requires non-empty steps")

    normalized: list[dict] = []
    ids: set[str] = set()
    missing: list[dict] = []
    graph: dict[str, set[str]] = {}
    outputs_by_step: dict[str, set[str]] = {}

    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"step {index + 1} must be an object")
        step_id = str(raw.get("step_id") or f"step_{index + 1}")
        if step_id in ids:
            raise ValueError(f"duplicate step_id: {step_id}")
        ids.add(step_id)
        script = str(raw.get("script_path") or "")
        if script not in entries:
            raise ValueError(f"unknown script_path: {script}")
        entry = entries[script]
        allowed = _declared_parameters(entry)
        bindings = raw.get("bindings") or {}
        if not isinstance(bindings, dict):
            raise ValueError(f"{step_id}.bindings must be an object")
        unknown = set(bindings) - allowed
        if unknown:
            raise ValueError(f"unknown parameter for {script}: {sorted(unknown)}")
        normalized_bindings: dict[str, dict] = {}
        for parameter, binding in bindings.items():
            if not isinstance(binding, dict):
                raise ValueError(f"binding {step_id}.{parameter} must be an object")
            source_type = str(binding.get("source_type") or "")
            if source_type not in SOURCE_TYPES:
                raise ValueError(f"unsupported source_type: {source_type}")
            normalized_bindings[str(parameter)] = dict(binding)
            if source_type in {"user_input", "derived_from_user_input", "default"} and "value" not in binding:
                missing.append({"step_id": step_id, "script_path": script, "input": str(parameter)})
            if source_type == "envelope" and input_envelope is not None:
                try:
                    source = str(binding.get("source") or "")
                    if source.split(".", 1)[0] not in _ENVELOPE_KEYS:
                        raise KeyError(source)
                    _lookup(input_envelope, source)
                except KeyError:
                    missing.append({"step_id": step_id, "script_path": script, "input": str(parameter)})

        for parameter in sorted(_required_parameters(entry) - set(bindings)):
            if parameter in (entry.get("default_values") or {}):
                normalized_bindings[parameter] = {
                    "source_type": "default", "source": parameter,
                    "value": entry["default_values"][parameter],
                }
            else:
                missing.append({"step_id": step_id, "script_path": script, "input": parameter})

        depends = raw.get("depends_on") or []
        if not isinstance(depends, list):
            raise ValueError(f"{step_id}.depends_on must be a list")
        graph[step_id] = {str(value) for value in depends}
        foreach = raw.get("foreach")
        if foreach is not None and not isinstance(foreach, dict):
            raise ValueError(f"{step_id}.foreach must be null or an object")
        normalized_step = {
            "step_id": step_id, "script_path": script,
            "description": str(raw.get("description") or ""),
            "bindings": normalized_bindings,
            "expected_outputs": list(raw.get("expected_outputs") or entry.get("outputs") or []),
            "depends_on": list(depends), "foreach": dict(foreach) if foreach else None,
        }
        invented_outputs = set(normalized_step["expected_outputs"]) - set(entry.get("outputs") or [])
        if invented_outputs:
            raise ValueError(f"unknown output for {script}: {sorted(invented_outputs)}")
        outputs_by_step[step_id] = set(entry.get("outputs") or [])
        normalized.append(normalized_step)

    positions = {step["step_id"]: i for i, step in enumerate(normalized)}
    for step in normalized:
        references = list(step["bindings"].values())
        if step["foreach"]:
            references.append(step["foreach"])
        for binding in references:
            if binding.get("source_type") != "step_output":
                continue
            upstream, output = _binding_reference(binding)
            if upstream not in positions:
                raise ValueError(f"unknown step_output step: {upstream}")
            graph[step["step_id"]].add(upstream)
            if positions[upstream] >= positions[step["step_id"]]:
                raise ValueError(f"future step reference: {step['step_id']} -> {upstream}")
            if output not in outputs_by_step[upstream]:
                raise ValueError(f"unknown output {output} from {upstream}")
        for dependency in graph[step["step_id"]]:
            if dependency not in positions:
                raise ValueError(f"unknown dependency: {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("runtime execution plan contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)

    return {
        "version": VERSION, "steps": normalized,
        "missing_required_inputs": missing,
        "warnings": list(plan.get("warnings") or []),
    }


def _lookup(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def resolve_binding(binding: dict, input_envelope: dict, runtime_context: dict,
                    resource_catalog: list[dict] | dict | None = None, *, item: Any = None) -> Any:
    source_type = binding["source_type"]
    if source_type in {"user_input", "derived_from_user_input", "default"}:
        if "value" not in binding:
            raise KeyError("binding value")
        return binding["value"]
    if source_type == "envelope":
        source = str(binding.get("source") or "")
        if source.split(".", 1)[0] not in _ENVELOPE_KEYS:
            raise KeyError(source)
        return _lookup(input_envelope, source)
    if source_type == "step_output":
        step_id, output = _binding_reference(binding)
        return _lookup(runtime_context["steps"][step_id], output)
    if source_type == "resource":
        handle = str(binding.get("source") or binding.get("resource_handle") or "")
        catalog = resource_catalog or []
        values = catalog.values() if isinstance(catalog, dict) else catalog
        for resource in values:
            if isinstance(resource, dict) and resource.get("resource_handle") == handle:
                return resource
        raise KeyError(handle)
    raise ValueError(f"unsupported source_type: {source_type}")


def resolve_step_bindings(step: dict, input_envelope: dict, runtime_context: dict,
                          resource_catalog: list[dict] | dict | None = None, *, item: Any = None) -> dict:
    """Resolve a validated step immediately before its real execution."""
    params = {}
    for name, binding in step.get("bindings", {}).items():
        if item is not None and step.get("foreach") and all(
            binding.get(key) == step["foreach"].get(key)
            for key in ("source_type", "step_id", "output", "source")
        ):
            params[name] = item
        else:
            params[name] = resolve_binding(binding, input_envelope, runtime_context, resource_catalog, item=item)
    return params
