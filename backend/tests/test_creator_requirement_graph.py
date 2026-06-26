import pytest

from backend.services.creator.common import (
    FileSpecOut,
    RequirementGraphValidationError,
    build_default_requirement_graph,
    parse_requirement_graph_result,
    normalize_requirement_graph,
    validate_requirement_graph_schema,
)
from backend.services.creator.repair import (
    _parse_requirement_review_result,
    detect_error_stdout_bypass,
)
from backend.services.runtime_tools.document_tools import create_pdf_document, create_text_file


def _script_spec(**kwargs):
    data = dict(
        path="scripts/generic.py",
        purpose="Transform the provided semantic input into the declared artifact.",
        required=True,
        can_skip=False,
        file_type="script",
        file_kind="script",
        inputs=["semantic source"],
        outputs=["semantic artifact"],
        artifact_contract={"final": True, "format": "portable"},
        required_capabilities=["generic_generation"],
    )
    data.update(kwargs)
    return FileSpecOut(**data)


def test_requirement_graph_generates_and_attaches_to_file_plan():
    spec = _script_spec()
    graph = validate_requirement_graph_schema(build_default_requirement_graph([spec]), [spec])
    by_file = {req.target_file: [req] for req in graph.requirements}
    spec.requirements = by_file[spec.path]
    assert spec.requirements[0].id
    assert spec.requirements[0].semantic_inputs == ["semantic source"]
    assert spec.requirements[0].semantic_outputs == ["semantic artifact"]


def test_requirement_graph_invalid_json_is_validator_error():
    with pytest.raises(RequirementGraphValidationError) as exc:
        parse_requirement_graph_result("not json")
    assert exc.value.code == "validator_error"


def test_requirement_graph_missing_required_requirement_is_incomplete():
    spec = _script_spec()
    graph = normalize_requirement_graph({"requirements": []})
    with pytest.raises(RequirementGraphValidationError) as exc:
        validate_requirement_graph_schema(graph, [spec])
    assert exc.value.code == "validator_incomplete"


def test_requirement_review_requires_check_coverage_and_missing_evidence_for_blocking():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    incomplete = _parse_requirement_review_result({"passed": True, "checks": []}, requirements=[req], file_path=req.target_file)
    assert incomplete["failure_type"] == "script_requirement_validator_incomplete"

    advisory_only = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "severity": "blocking", "evidence_level": "weak", "missing_evidence": ["x"]}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert advisory_only["passed"] is True

    failed = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "severity": "blocking", "evidence_level": "missing", "missing_evidence": ["required component"]}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert failed["failure_type"] == "script_requirement_failed"
    assert failed["issues"][0]["requirement_id"] == req.id


def test_error_stdout_bypass_cannot_satisfy_expected_outputs():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    issues = detect_error_stdout_bypass('try:\n    run()\nexcept Exception:\n    return {"error": "failed"}\n', [req], ["semantic artifact"])
    assert issues and issues[0]["requirement_id"] == req.id


def test_runtime_metadata_contains_generic_requirement_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
    result = create_pdf_document(
        [{"type": "heading", "level": 2, "text": "H"}, {"type": "image", "path": str(tmp_path / "a.png")}, {"type": "table", "headers": ["a"], "rows": [[1]]}],
        output_dir=tmp_path,
        styles={"margin": 10, "line_spacing": 1.2},
        filename="out.pdf",
    )
    meta = result["artifact_metadata"]
    for key in ["creator_tool", "block_count", "block_types", "component_types", "styles", "options", "referenced_paths", "media_items", "table_items", "heading_levels", "layout_options", "constraint_values"]:
        assert key in meta
    assert "image" in meta["component_types"]

    text_result = create_text_file("hello", output_dir=tmp_path)
    assert text_result["artifact_metadata"]["component_types"] == ["text"]
