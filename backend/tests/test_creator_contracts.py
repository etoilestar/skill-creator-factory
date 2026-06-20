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
