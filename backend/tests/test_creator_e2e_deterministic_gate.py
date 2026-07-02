from backend.services.creator.api import _split_e2e_blocking_errors, _normalize_file_plan_for_requirement_coverage, PreparePlanReviewSummary
from backend.services.creator.common import FileSpecOut


def test_validator_target_is_warning_not_blocking():
    error = (
        "E2E_REPAIR_TARGET=__validator__\n"
        "E2E_LAYER=e2e_requirement_validator_error\n"
        "E2E_STRUCTURED_FAILURE={\"target_file\":\"__validator__\",\"layer\":\"e2e_requirement_validator_error\"}\n"
        "validator HTTP 500"
    )
    blocking, warnings = _split_e2e_blocking_errors([error])
    assert blocking == []
    assert warnings and warnings[0]["target_file"] == "__validator__"


def test_script_exit_remains_blocking():
    error = (
        "E2E_REPAIR_TARGET=scripts/run.py\n"
        "E2E_LAYER=script_exit\n"
        "E2E_STRUCTURED_FAILURE={\"target_file\":\"scripts/run.py\",\"layer\":\"script_exit\"}\n"
        "return_code=1"
    )
    blocking, warnings = _split_e2e_blocking_errors([error])
    assert blocking == [error]
    assert warnings == []


def test_single_script_gets_full_coverage_contract():
    spec = FileSpecOut(
        path="scripts/extract_summary.py",
        generation_order=1,
        purpose="summarize uploaded document",
        required=True,
        can_skip=False,
        file_type="script",
        file_kind="script",
        role="generic_script",
        inputs=["input_file"],
        outputs=["summary"],
        language="python",
        runtime="python",
        entrypoint="scripts/extract_summary.py",
    )
    warnings = []
    _normalize_file_plan_for_requirement_coverage(
        blueprint_text="支持上传 PDF DOCX TXT，生成 paragraph_summaries section_summaries document_summary，输出 JSON Markdown。",
        review_summary=PreparePlanReviewSummary(input="uploaded PDF DOCX TXT", output="JSON Markdown summaries", workflow=["parse", "summarize", "render"]),
        files_out=[spec],
        final_outputs=["markdown", "json"],
        warnings=warnings,
    )
    coverage = spec.runtime_contract.get("coverage_requirements")
    assert coverage
    assert coverage.get("single_script_full_coverage_contract") is True
    assert any("coverage:" in item for item in spec.inputs)
    assert any(item["code"] == "single_script_full_coverage_contract" for item in warnings)
