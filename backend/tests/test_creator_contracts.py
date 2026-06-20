from backend.services.creator_contracts import (
    compile_canonical_file_contract,
    resolve_implementation,
    validate_python_evidence,
)
from backend.services.skill_plan import SkillPlanEntry


def _entry(**kw):
    data = dict(
        path="scripts/main.py",
        role="generic_script",
        file_type="script",
        purpose="test",
        runtime="python",
        inputs=["payload"],
        outputs=["result"],
        dependencies=[],
        required_capabilities=[],
    )
    data.update(kw)
    return SkillPlanEntry(**data)


def _schema():
    return {"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}}


def test_creator_implemented_rejects_literal_shell_script():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
def run(payload):
    return {"result": "fixed"}
"""

    issues = validate_python_evidence(code, contract, resolution)

    assert resolution.mode == "creator_implemented"
    assert any(issue.startswith("input_dependency") for issue in issues)
    assert any(issue.startswith("nontrivial_transform") for issue in issues)


def test_creator_implemented_accepts_input_dependent_transform():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
import json

def run(payload):
    text = json.dumps(payload.get("payload", ""), ensure_ascii=False).strip()
    return {"result": text}
"""

    assert validate_python_evidence(code, contract, resolution) == []


def test_registered_tool_requires_selected_call():
    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path", "file_outputs"])
    schema = {"type": "object", "required": ["pdf_path", "file_outputs"], "properties": {}}
    contract = compile_canonical_file_contract(entry, schema)
    resolution = resolve_implementation(entry, contract)

    assert resolution.mode == "use_registered_tool"
    issues = validate_python_evidence("def run(payload):\n    return {'pdf_path': 'x.pdf', 'file_outputs': ['x.pdf']}\n", contract, resolution)
    assert any(issue.startswith("tool_call") for issue in issues)

    ok_code = """
from backend.services.runtime_tools import create_pdf

def run(payload):
    result = create_pdf(text=str(payload.get("payload", "")))
    return {"pdf_path": result["pdf_path"], "file_outputs": result.get("file_outputs")}
"""
    assert validate_python_evidence(ok_code, contract, resolution) == []


def test_undeclared_third_party_import_fails():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
import requests

def run(payload):
    value = str(payload.get("payload", "")).strip()
    return {"result": value}
"""

    assert any(issue.startswith("declared_dependency_only") for issue in validate_python_evidence(code, contract, resolution))


def test_configured_discovery_adapter_loads_callable_manifests():
    from backend.services.creator_tool_discovery import discover_creator_tool_records

    records = discover_creator_tool_records({"registries": ["backend/config/tool_registry.custom.json"], "modules": []})

    assert records
    assert all(record.get("functions") for record in records)
    assert all("input_schema" in record and "output_schema" in record for record in records)
    assert all("artifact_outputs" in record and "side_effects" in record for record in records)


def test_selected_tool_schema_refines_canonical_contract():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["pdf_path"], "properties": {"pdf_path": {}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "use_registered_tool"
    assert "pdf_path" in refined.outputs
    assert "file_outputs" in refined.stdout_schema["required"]


def test_references_and_assets_are_not_script_io_keys():
    entry = _entry(inputs=["payload", "references/guide.md", "assets/source.png"], outputs=["result", "assets/generated/out.png"])
    contract = compile_canonical_file_contract(entry, _schema())

    assert contract.inputs == ["payload"]
    assert contract.outputs == ["result"]


def test_system_manifest_selects_text_generation_tool():
    entry = _entry(role="text_generator", required_capabilities=["text_generation"], outputs=["text"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)

    assert resolution.mode == "use_registered_tool"
    assert any(tool.function_name == "generate_text_with_llm" for tool in resolution.selected_tools)


def test_system_manifest_selects_image_generation_artifact_tool():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="image_generator", required_capabilities=["image_generation"], outputs=["image_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["image_path"], "properties": {"image_path": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "use_registered_tool"
    assert any(tool.function_name == "generate_stable_diffusion_image" for tool in resolution.selected_tools)
    assert "artifact_created" in resolution.required_evidence
    assert refined.artifact_contract.get("tool_artifact_outputs")


def test_raw_capability_hints_are_candidate_signals_for_tool_resolution():
    entry = _entry(role="text_generator", required_capabilities=[], raw_capability_hints=["text_generation"], outputs=["text"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)

    assert any(req.capability_id == "text_generation" and req.source == "raw_capability_hints" and not req.required for req in contract.capability_requirements)
    assert resolution.mode == "use_registered_tool"
    assert any(tool.function_name == "generate_text_with_llm" for tool in resolution.selected_tools)
