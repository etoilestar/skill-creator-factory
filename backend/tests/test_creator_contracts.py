from backend.services.creator_contracts import (
    compile_canonical_file_contract,
    resolve_implementation,
    validate_python_evidence,
)
from backend.services.skill_plan import SkillPlanEntry, ToolSlot


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


def test_script_composition_rejects_literal_shell_script():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
def run(payload):
    return {"result": "fixed"}
"""

    issues = validate_python_evidence(code, contract, resolution)

    assert resolution.mode == "script_composition"
    assert any(issue.startswith("input_dependency") for issue in issues)
    assert any(issue.startswith("nontrivial_transform") for issue in issues)


def test_script_composition_accepts_input_dependent_transform():
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

    assert resolution.mode == "script_composition"
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


def test_available_tool_schema_does_not_replace_canonical_contract():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["pdf_path"], "properties": {"pdf_path": {}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "script_composition"
    assert "pdf_path" in refined.outputs
    assert refined.stdout_schema["required"] == ["pdf_path"]


def test_references_and_assets_are_not_script_io_keys():
    entry = _entry(
        inputs=["payload", "references/guide.md", "assets/source.png"],
        outputs=["result", "assets/generated/out.png"],
        dependencies=["requests", "references/guide.md", "assets/source.png"],
    )
    contract = compile_canonical_file_contract(entry, _schema())

    assert contract.inputs == ["payload"]
    assert contract.outputs == ["result"]
    assert contract.declared_dependencies == ["requests"]


def test_system_manifest_selects_text_generation_tool():
    entry = _entry(role="text_generator", required_capabilities=["text_generation"], outputs=["text"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)

    assert resolution.mode == "script_composition"
    assert any(tool.function_name == "generate_text_with_llm" for tool in resolution.selected_tools)


def test_system_manifest_selects_image_generation_artifact_tool():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="image_generator", required_capabilities=["image_generation"], outputs=["image_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["image_path"], "properties": {"image_path": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "script_composition"
    assert any(tool.function_name == "generate_stable_diffusion_image" for tool in resolution.selected_tools)
    assert "artifact_created" in resolution.required_evidence
    assert not refined.artifact_contract.get("tool_artifact_outputs")


def test_raw_capability_hints_are_candidate_signals_for_tool_resolution():
    entry = _entry(role="text_generator", required_capabilities=[], raw_capability_hints=["text_generation"], outputs=["text"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)

    assert any(req.capability_id == "text_generation" and req.source == "raw_capability_hints" and not req.required for req in contract.capability_requirements)
    assert resolution.mode == "script_composition"
    assert any(tool.function_name == "generate_text_with_llm" for tool in resolution.selected_tools)


def test_single_text_output_mapping_allows_business_stdout_field():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="text_generator", required_capabilities=[], raw_capability_hints=["text_generation"], outputs=["article_body"])
    schema = {"type": "object", "required": ["article_body"], "properties": {"article_body": {"type": "string"}}}
    contract = compile_canonical_file_contract(entry, schema)
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "script_composition"
    assert resolution.output_mappings == [{"source_tool_field": "text", "target_stdout_field": "article_body"}]
    assert refined.stdout_schema["required"] == ["article_body"]
    assert "text" not in refined.outputs


def test_trial_stdout_keeps_canonical_required_fields():
    from backend.routers.creator import _validate_trial_stdout_json
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["pdf_path"], "properties": {"pdf_path": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert refined.stdout_schema["required"] == ["pdf_path"]
    _validate_trial_stdout_json(stdout='{"pdf_path":"outputs/a.pdf"}', content="", args=["{}"], canonical_contract=refined)


def test_partial_tool_schema_becomes_available_tool_candidate():
    from backend.services.creator_tool_registry import ToolCapability, ToolFunctionManifest, clear_registered_tool_capabilities, register_tool_capability

    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="lookup_helper",
        display_name="Lookup Helper",
        category="retrieval",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools",
            short_description="Lookup one source value.",
            when_to_use="Use for the lookup functional step.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            output_schema={"type": "object", "required": ["source_value"], "properties": {"source_value": {"type": "string"}}},
            required_capabilities=["lookup_helper"],
        )],
    ))
    try:
        entry = _entry(
            raw_capability_hints=["lookup_helper"],
            outputs=["final_answer", "confidence"],
            required_tool_slots=[ToolSlot(slot_id="lookup", functional_requirement="lookup source data")],
        )
        schema = {"type": "object", "required": ["final_answer", "confidence"], "properties": {"final_answer": {"type": "string"}, "confidence": {"type": "number"}}}
        contract = compile_canonical_file_contract(entry, schema)
        resolution = resolve_implementation(entry, contract)

        assert contract.functional_requirements == ["lookup source data"]
        assert resolution.mode == "script_composition"
        assert resolution.available_tools[0].tool_id == "lookup_helper.lookup_value"
        assert resolution.tool_slots[0]["tool_id"] == "lookup_helper.lookup_value"
        assert "tool_result_used" in resolution.required_evidence
        assert resolution.allowed_imports == ["backend.services.runtime_tools"]
    finally:
        clear_registered_tool_capabilities()
