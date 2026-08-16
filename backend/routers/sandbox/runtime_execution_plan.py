"""Strict Sandbox-only runtime plan protocol and binding resolver."""

from __future__ import annotations

import re
from typing import Any

VERSION = "sandbox-runtime-plan/v1"
SOURCE_TYPES = {"envelope", "user_input", "derived_from_user_input", "default", "step_output", "resource"}
ENVELOPE_ROOTS = {"user_request", "input", "text", "payload", "fields", "options", "input_files", "files", "resources"}
TOP_LEVEL_FIELDS = {"version", "steps", "missing_required_inputs", "warnings"}
STEP_FIELDS = {"step_id", "script_path", "description", "bindings", "expected_outputs", "depends_on", "foreach"}
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RuntimePlanError(ValueError):
    """A machine-classifiable Sandbox runtime-plan protocol error."""

    def __init__(self, code: str, message: str, *, details: Any = None):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str, details: Any = None) -> None:
    raise RuntimePlanError(code, message, details=details)


def _entries(action_schema: dict) -> dict[str, dict]:
    return {str(e.get("script_path")): e for e in action_schema.get("entries") or [] if isinstance(e, dict) and e.get("script_path")}


def _parameters(entry: dict) -> set[str]:
    return {str(v) for v in [*(entry.get("inputs") or []), *(entry.get("optional_inputs") or []), *(entry.get("command_keys") or [])] if v}


def _required(entry: dict) -> set[str]:
    return set(entry.get("inputs") or []) - set(entry.get("optional_inputs") or [])


def _lookup(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit():
                raise KeyError(path)
            index = int(part)
            if index >= len(current):
                raise KeyError(path)
            current = current[index]
        else:
            raise KeyError(path)
    return current


def _valid_envelope_source(source: Any) -> bool:
    return isinstance(source, str) and bool(source) and source.split(".", 1)[0] in ENVELOPE_ROOTS


def _canonical_binding(parameter: str, binding: Any, entry: dict, envelope: dict | None) -> dict:
    if not isinstance(binding, dict):
        _fail("runtime_plan_invalid_binding", f"binding {parameter} must be an object")
    source_type = binding.get("source_type")
    if source_type not in SOURCE_TYPES:
        _fail("runtime_plan_invalid_binding", f"binding {parameter} has invalid source_type")
    shapes = {
        "envelope": ({"source_type", "source"}, {"source_type", "source"}),
        "user_input": ({"source_type", "source", "value"}, {"source_type", "value"}),
        "derived_from_user_input": ({"source_type", "source", "value"}, {"source_type", "value"}),
        # Compatibility accepts a planner value but deliberately discards it;
        # Action Schema remains the only source of truth.
        "default": ({"source_type", "source", "value"}, {"source_type"}),
        "step_output": ({"source_type", "step_id", "output"}, {"source_type", "step_id", "output"}),
        "resource": ({"source_type", "source", "resource_handle"}, {"source_type"}),
    }
    allowed, required = shapes[source_type]
    unknown = set(binding) - allowed
    missing = required - set(binding)
    if unknown or missing:
        _fail("runtime_plan_invalid_binding", f"binding {parameter} shape is invalid", {"unknown": sorted(unknown), "missing": sorted(missing)})
    if source_type == "envelope":
        if not _valid_envelope_source(binding["source"]):
            _fail("runtime_plan_invalid_envelope_source", f"invalid envelope source for {parameter}")
        return dict(binding)
    if source_type in {"user_input", "derived_from_user_input"}:
        source = binding.get("source", "user_request")
        if not _valid_envelope_source(source):
            _fail("runtime_plan_invalid_envelope_source", f"invalid user input source for {parameter}")
        # Explicit same-name structured values are canonical and cannot be
        # overridden by a natural-language derivation.
        if envelope is not None:
            for root in ("fields", "options"):
                values = envelope.get(root)
                if isinstance(values, dict) and parameter in values:
                    return {"source_type": "envelope", "source": f"{root}.{parameter}"}
        return {"source_type": source_type, "source": source, "value": binding["value"]}
    if source_type == "default":
        defaults = entry.get("default_values") or {}
        if parameter not in defaults:
            _fail("runtime_plan_missing_input", f"no Action Schema default for {parameter}")
        return {"source_type": "default", "source": "action_schema.default_values", "value": defaults[parameter]}
    if source_type == "step_output":
        if not STEP_ID_RE.fullmatch(str(binding["step_id"])) or not str(binding["output"]):
            _fail("runtime_plan_invalid_dependency", f"invalid step_output binding for {parameter}")
        return dict(binding)
    handle = binding.get("resource_handle") or binding.get("source")
    if not isinstance(handle, str) or not handle:
        _fail("runtime_plan_missing_resource", f"resource binding {parameter} requires a handle")
    return {"source_type": "resource", "resource_handle": handle}


def validate_runtime_execution_plan(plan: dict, action_schema: dict, input_envelope: dict | None = None,
                                    resource_catalog: list[dict] | dict | None = None) -> dict:
    """Return an Action-Schema-canonical plan or raise ``RuntimePlanError``."""
    if not isinstance(plan, dict):
        _fail("runtime_plan_invalid_protocol", "plan must be a JSON object")
    if not isinstance(plan.get("version"), str) or plan.get("version") != VERSION:
        _fail("runtime_plan_invalid_version", f"version must equal {VERSION}")
    unknown_top = set(plan) - TOP_LEVEL_FIELDS
    if unknown_top:
        _fail("unknown_runtime_plan_field", "unknown top-level fields", sorted(unknown_top))
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        _fail("runtime_plan_invalid_protocol", "steps must be a non-empty list")

    entries = _entries(action_schema)
    if not isinstance(plan.get("warnings", []), list) or not isinstance(plan.get("missing_required_inputs", []), list):
        _fail("runtime_plan_invalid_protocol", "warnings and missing_required_inputs must be lists")
    steps, ids, missing, warnings = [], set(), [], list(plan.get("warnings") or [])
    catalog_values = ((resource_catalog or {}).values() if isinstance(resource_catalog, dict) else (resource_catalog or []))
    resource_handles = {item.get("resource_handle") for item in catalog_values if isinstance(item, dict)}
    outputs_by_id: dict[str, set[str]] = {}
    graph: dict[str, set[str]] = {}
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            _fail("runtime_plan_invalid_protocol", f"step {index + 1} must be an object")
        unknown_step = set(raw) - STEP_FIELDS
        if unknown_step:
            _fail("runtime_plan_unknown_step_field", f"unknown fields in step {index + 1}", sorted(unknown_step))
        step_id = raw.get("step_id")
        if not isinstance(step_id, str) or not STEP_ID_RE.fullmatch(step_id):
            _fail("runtime_plan_invalid_step_id", f"invalid step_id at index {index}")
        if step_id in ids:
            _fail("runtime_plan_invalid_step_id", f"duplicate step_id: {step_id}")
        ids.add(step_id)
        script = raw.get("script_path")
        if not isinstance(script, str) or script not in entries:
            _fail("runtime_plan_unknown_script", f"unknown script_path: {script}")
        entry = entries[script]
        bindings = raw.get("bindings")
        if not isinstance(bindings, dict):
            _fail("runtime_plan_invalid_binding", f"{step_id}.bindings must be an object")
        unknown_params = set(bindings) - _parameters(entry)
        if unknown_params:
            _fail("runtime_plan_unknown_parameter", f"unknown parameters for {script}", sorted(unknown_params))
        canonical_bindings = {name: _canonical_binding(name, value, entry, input_envelope) for name, value in bindings.items()}
        if resource_catalog is not None:
            for name, binding in canonical_bindings.items():
                if binding["source_type"] == "resource" and binding["resource_handle"] not in resource_handles:
                    _fail("runtime_plan_missing_resource", f"resource not found for {name}")
        if input_envelope is not None:
            for name, binding in canonical_bindings.items():
                if binding["source_type"] == "envelope":
                    try:
                        _lookup(input_envelope, binding["source"])
                    except KeyError:
                        missing.append({"step_id": step_id, "script_path": script, "input": name,
                                        "code": "runtime_plan_missing_input"})
        for name in sorted(_required(entry) - set(canonical_bindings)):
            if name in (entry.get("default_values") or {}):
                canonical_bindings[name] = _canonical_binding(name, {"source_type": "default"}, entry, input_envelope)
            else:
                missing.append({"step_id": step_id, "script_path": script, "input": name, "code": "runtime_plan_missing_input"})
        declared_outputs = list(entry.get("outputs") or [])
        supplied_outputs = raw.get("expected_outputs")
        if supplied_outputs is not None and supplied_outputs != declared_outputs:
            _fail("runtime_plan_output_contract_mismatch", f"expected_outputs for {script} must match Action Schema")
        explicit = raw.get("depends_on", [])
        if not isinstance(explicit, list) or any(not isinstance(v, str) for v in explicit):
            _fail("runtime_plan_invalid_dependency", f"{step_id}.depends_on must contain step ids")
        foreach = raw.get("foreach")
        canonical_foreach = _canonical_binding("foreach", foreach, {"default_values": {}}, input_envelope) if foreach is not None else None
        dependencies = set(explicit)
        for ref in [*canonical_bindings.values(), *([canonical_foreach] if canonical_foreach else [])]:
            if ref.get("source_type") == "step_output":
                dependencies.add(ref["step_id"])
        graph[step_id] = dependencies
        outputs_by_id[step_id] = set(declared_outputs)
        if not isinstance(raw.get("description", ""), str):
            _fail("runtime_plan_invalid_protocol", f"{step_id}.description must be a string")
        steps.append({"step_id": step_id, "script_path": script, "description": raw.get("description", ""),
                      "bindings": canonical_bindings, "expected_outputs": declared_outputs,
                      "depends_on": sorted(dependencies), "foreach": canonical_foreach})

    positions = {step["step_id"]: index for index, step in enumerate(steps)}
    for step in steps:
        for ref in [*step["bindings"].values(), *([step["foreach"]] if step["foreach"] else [])]:
            if ref.get("source_type") != "step_output":
                continue
            upstream, output = ref["step_id"], ref["output"]
            if upstream not in positions:
                _fail("runtime_plan_invalid_dependency", f"unknown step: {upstream}")
            if positions[upstream] >= positions[step["step_id"]]:
                _fail("runtime_plan_future_reference", f"{step['step_id']} references future step {upstream}")
            if output not in outputs_by_id[upstream]:
                _fail("runtime_plan_unknown_output", f"unknown output {output} from {upstream}")
        if any(dep not in positions for dep in step["depends_on"]):
            _fail("runtime_plan_invalid_dependency", f"unknown dependency in {step['step_id']}")

    visiting, visited = set(), set()
    def visit(node: str) -> None:
        if node in visiting:
            _fail("runtime_plan_cycle", "dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node); visited.add(node)
    for node in graph:
        visit(node)
    for step in steps:
        if any(positions[dep] >= positions[step["step_id"]] for dep in step["depends_on"]):
            _fail("runtime_plan_future_reference", f"{step['step_id']} has a future dependency")
    return {"version": VERSION, "steps": steps, "missing_required_inputs": missing, "warnings": warnings}


def resolve_binding(binding: dict, input_envelope: dict, runtime_context: dict,
                    resource_catalog: list[dict] | dict | None = None, *, item: Any = None) -> Any:
    source_type = binding["source_type"]
    if source_type in {"user_input", "derived_from_user_input", "default"}:
        return binding["value"]
    if source_type == "envelope":
        return _lookup(input_envelope, binding["source"])
    if source_type == "step_output":
        return _lookup(runtime_context["steps"][binding["step_id"]], binding["output"])
    if source_type == "resource":
        handle = binding["resource_handle"]
        values = (resource_catalog or {}).values() if isinstance(resource_catalog, dict) else (resource_catalog or [])
        for resource in values:
            if isinstance(resource, dict) and resource.get("resource_handle") == handle:
                return resource
        _fail("runtime_plan_missing_resource", f"resource not found: {handle}")
    _fail("runtime_plan_invalid_binding", f"unsupported source_type: {source_type}")


def resolve_step_bindings(step: dict, input_envelope: dict, runtime_context: dict,
                          resource_catalog: list[dict] | dict | None = None, *, item: Any = None) -> dict:
    params = {}
    for name, binding in step["bindings"].items():
        if item is not None and step.get("foreach") == binding:
            params[name] = item
        else:
            params[name] = resolve_binding(binding, input_envelope, runtime_context, resource_catalog, item=item)
    return params
