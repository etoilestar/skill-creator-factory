"""Canonical Creator script contracts and evidence validation.

The code here is intentionally generic: it compiles normalized SkillPlan data
into a per-file canonical contract, resolves whether a callable registered tool
or Creator-owned local implementation should be used, and validates generated
Python source with AST evidence rather than business field names.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .creator_tool_registry import ToolCapability, list_tool_capabilities
from .skill_plan import SkillPlanEntry

ImplementationMode = Literal["use_registered_tool", "creator_implemented", "unresolved"]


@dataclass(frozen=True)
class CallableToolManifest:
    tool_id: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    artifact_outputs: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    import_path: str = ""
    function_name: str = ""
    dependencies: list[str] = field(default_factory=list)
    example_call: str = ""


@dataclass(frozen=True)
class CapabilityRequirement:
    capability_id: str
    source: str = "skill_plan"
    required: bool = True


@dataclass(frozen=True)
class CanonicalFileContract:
    file_path: str
    file_kind: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    stdout_schema: dict[str, Any] = field(default_factory=dict)
    artifact_contract: dict[str, Any] = field(default_factory=dict)
    capability_requirements: list[CapabilityRequirement] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    resource_refs: list[str] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    upstream_dependencies: list[str] = field(default_factory=list)
    downstream_consumers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImplementationResolution:
    mode: ImplementationMode
    selected_tools: list[CallableToolManifest] = field(default_factory=list)
    selected_adapters: list[str] = field(default_factory=list)
    local_fallback_allowed: bool = True
    allowed_imports: list[str] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    reason: str = ""


def _schema_required(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("required") if isinstance(schema, dict) else []
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _dependency_import_roots(dependencies: list[Any]) -> list[str]:
    roots: list[str] = []
    for dep in dependencies or []:
        if isinstance(dep, dict):
            imports = dep.get("imports") or dep.get("import_names") or []
            if isinstance(imports, str):
                imports = [imports]
            for item in imports:
                root = str(item).split(".")[0].replace("-", "_").strip()
                if root and root not in roots:
                    roots.append(root)
            package = str(dep.get("package") or dep.get("name") or "").strip()
            if package:
                root = package.split(".")[0].replace("-", "_")
                if root and root not in roots:
                    roots.append(root)
        elif isinstance(dep, str) and dep.strip():
            root = dep.split(".")[0].replace("-", "_").strip()
            if root and root not in roots:
                roots.append(root)
    return roots


def callable_manifest_from_capability(cap: ToolCapability) -> list[CallableToolManifest]:
    manifests: list[CallableToolManifest] = []
    for fn in cap.functions or []:
        if not fn.import_path or not fn.function_name:
            continue
        deps = _dependency_import_roots(list(cap.dependencies or []))
        manifests.append(CallableToolManifest(
            tool_id=f"{cap.name}.{fn.function_name}",
            description=fn.when_to_use or fn.short_description or cap.display_name,
            input_schema=fn.input_schema or cap.input_schema or {},
            output_schema=fn.output_schema or cap.output_schema or {},
            artifact_outputs=getattr(fn, "artifact_outputs", []) or getattr(cap, "artifact_outputs", []) or [],
            side_effects=list(getattr(fn, "side_effects", []) or getattr(cap, "side_effects", []) or []),
            import_path=fn.import_path,
            function_name=fn.function_name,
            dependencies=deps,
            example_call=fn.example_call,
        ))
    return manifests


def compile_canonical_file_contract(entry: SkillPlanEntry, stdout_schema: dict[str, Any]) -> CanonicalFileContract:
    requirements = [CapabilityRequirement(str(c)) for c in (entry.required_capabilities or []) if c]
    artifact_contract = getattr(entry, "artifact_contract", {}) or {"stdout_fields": list(entry.outputs or [])}
    return CanonicalFileContract(
        file_path=entry.path,
        file_kind=getattr(entry, "file_kind", entry.file_type),
        inputs=list(entry.inputs or []),
        outputs=list(entry.outputs or []),
        stdout_schema=stdout_schema,
        artifact_contract=artifact_contract,
        capability_requirements=requirements,
        side_effects=list(getattr(entry, "side_effects", []) or []),
        resource_refs=[getattr(r, "path", str(r)) for r in (getattr(entry, "resources", []) or [])],
        declared_dependencies=list(entry.dependencies or []),
        upstream_dependencies=list(getattr(entry, "upstream_dependencies", []) or []),
        downstream_consumers=list(getattr(entry, "downstream_consumers", []) or []),
    )


def resolve_implementation(entry: SkillPlanEntry, contract: CanonicalFileContract) -> ImplementationResolution:
    manifests: list[CallableToolManifest] = []
    capability_ids = {req.capability_id for req in contract.capability_requirements if req.capability_id}
    for cap in list_tool_capabilities():
        if not cap.enabled_by_default or not cap.allow_creator_use:
            continue
        cap_manifests = callable_manifest_from_capability(cap)
        if not cap_manifests:
            continue
        if not _capability_matches_contract(cap, capability_ids, contract):
            continue
        manifests.extend(cap_manifests)
    if manifests:
        imports = [m.import_path.split(".")[0] for m in manifests if m.import_path]
        deps = sorted({d for m in manifests for d in m.dependencies})
        return ImplementationResolution(
            mode="use_registered_tool",
            selected_tools=manifests,
            local_fallback_allowed=False,
            allowed_imports=sorted({"json", "sys", "os", "pathlib", "typing", "backend", *imports, *deps}),
            declared_dependencies=sorted(set(contract.declared_dependencies + deps)),
            required_evidence=["tool_call", "stdout_from_tool", "declared_dependency_only", "stdout_contract", "no_shell_template"],
            reason="Callable Tool Registry manifest satisfies the canonical capability contract.",
        )
    if contract.file_path and contract.stdout_schema and (contract.outputs or _schema_required(contract.stdout_schema)):
        return ImplementationResolution(
            mode="creator_implemented",
            selected_tools=[],
            local_fallback_allowed=True,
            allowed_imports=sorted({"json", "sys", "os", "pathlib", "typing", "datetime", "csv", "math", "statistics", "re", "html", "hashlib", "itertools", "collections", *contract.declared_dependencies}),
            declared_dependencies=list(contract.declared_dependencies),
            required_evidence=["declared_dependency_only", "input_dependency", "nontrivial_transform", "stdout_contract", "no_shell_template"],
            reason="No callable tool manifest matched; Creator may implement with standard library/local code subject to evidence validation.",
        )
    return ImplementationResolution(mode="unresolved", local_fallback_allowed=False, required_evidence=[], reason="Canonical contract is incomplete.")


def _capability_matches_contract(cap: ToolCapability, capability_ids: set[str], contract: CanonicalFileContract) -> bool:
    """Structurally match tool manifests to the canonical contract.

    Capability ids are structured hints from SkillPlan; schema compatibility is
    checked against output requirements. This intentionally avoids file names,
    business keywords, and role-specific special cases.
    """
    tool_ids = {cap.name, *cap.required_capabilities, *cap.optional_capabilities}
    fn_required_caps = {item for fn in cap.functions for item in (fn.required_capabilities or [])}
    if capability_ids and not (capability_ids & (tool_ids | fn_required_caps)):
        return False
    required_stdout = set(_schema_required(contract.stdout_schema) or contract.outputs)
    if not required_stdout:
        return True
    tool_output_fields: set[str] = set()
    for fn in cap.functions or []:
        schema = fn.output_schema or cap.output_schema or {}
        props = schema.get("properties") if isinstance(schema, dict) else {}
        if isinstance(props, dict):
            tool_output_fields.update(str(key) for key in props.keys())
        tool_output_fields.update(_schema_required(schema))
    return bool(required_stdout & tool_output_fields) or bool(capability_ids & (tool_ids | fn_required_caps))


def contract_payload(contract: CanonicalFileContract, resolution: ImplementationResolution) -> dict[str, Any]:
    return {"canonical_contract": asdict(contract), "implementation_resolution": asdict(resolution)}


class _EvidenceVisitor(ast.NodeVisitor):
    def __init__(self, resolution: ImplementationResolution):
        self.resolution = resolution
        self.import_roots: set[str] = set()
        self.calls: set[str] = set()
        self.has_file_write = False
        self.has_transform_call = False
        self.input_refs = 0
        self.constants_in_returns = 0
        self.nonconstants_in_returns = 0

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            if alias.name:
                self.import_roots.add(alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        if node.module:
            self.import_roots.add(node.module.split(".")[0])

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)
        if name:
            self.calls.add(name)
            if name.split(".")[-1] in {"write", "write_text", "write_bytes", "open", "dump", "dumps", "loads", "join", "format", "append", "extend", "update", "read", "read_text", "read_bytes"}:
                self.has_transform_call = True
            if name.split(".")[-1] in {"write", "write_text", "write_bytes"}:
                self.has_file_write = True
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        if _name_root(node.value) in {"payload", "data", "argv", "args"}:
            self.input_refs += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if _name_root(node.value) in {"payload", "data", "argv", "args"}:
            self.input_refs += 1
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> Any:
        for child in ast.walk(node.value) if node.value is not None else []:
            if isinstance(child, (ast.Constant, ast.JoinedStr)):
                self.constants_in_returns += 1
            elif isinstance(child, (ast.Name, ast.Call, ast.Subscript, ast.Attribute, ast.BinOp, ast.DictComp, ast.ListComp)):
                self.nonconstants_in_returns += 1
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _name_root(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name_root(node.value)
    if isinstance(node, ast.Subscript):
        return _name_root(node.value)
    return ""


def validate_python_evidence(content: str, contract: CanonicalFileContract, resolution: ImplementationResolution) -> list[str]:
    if resolution.mode == "unresolved":
        return ["implementation_resolution_unresolved: unresolved scripts must not be generated"]
    try:
        tree = ast.parse(content or "")
    except SyntaxError as exc:
        return [f"python_ast_invalid: {exc}"]
    visitor = _EvidenceVisitor(resolution)
    visitor.visit(tree)
    issues: list[str] = []
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    allowed = set(resolution.allowed_imports) | {d.split(".")[0].replace("-", "_") for d in resolution.declared_dependencies}
    undeclared = sorted(root for root in visitor.import_roots if root not in stdlib and root not in allowed)
    if undeclared:
        issues.append("declared_dependency_only: undeclared imports " + ", ".join(undeclared))
    required = set(_schema_required(contract.stdout_schema) or contract.outputs)
    if required:
        source = content or ""
        missing = [key for key in required if key not in source]
        if missing:
            issues.append("stdout_contract: source does not mention required stdout fields " + ", ".join(missing))
    selected_functions = {m.function_name for m in resolution.selected_tools if m.function_name}
    selected_imports = {m.import_path for m in resolution.selected_tools if m.import_path}
    if resolution.mode == "use_registered_tool":
        has_tool_call = bool(selected_functions & {c.split(".")[-1] for c in visitor.calls})
        has_tool_import = any(path.split(".")[0] in visitor.import_roots or path in content for path in selected_imports)
        if not (has_tool_call and has_tool_import):
            issues.append("tool_call: selected callable tool was not imported and called")
        if visitor.nonconstants_in_returns == 0:
            issues.append("stdout_from_tool: stdout does not appear to depend on computed/tool values")
    elif resolution.mode == "creator_implemented":
        if visitor.input_refs == 0:
            issues.append("input_dependency: outputs do not appear to depend on argv/payload input")
        if not (visitor.has_transform_call or visitor.has_file_write):
            issues.append("nontrivial_transform: no parsing/transformation/file-processing evidence found")
    if visitor.nonconstants_in_returns == 0 and visitor.constants_in_returns > 0:
        issues.append("no_shell_template: required outputs appear to be literal-only")
    return issues
