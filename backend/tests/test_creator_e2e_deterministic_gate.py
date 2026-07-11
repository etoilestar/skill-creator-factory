from backend.services.creator.api import (
    PreparePlanReviewSummary,
    _normalize_file_plan_for_requirement_coverage,
    _split_e2e_blocking_errors,
)
from backend.services.creator.common import FileSpecOut


def _script_spec(path="scripts/extract_summary.py", purpose="summarize uploaded document"):
    return FileSpecOut(
        path=path,
        generation_order=1,
        purpose=purpose,
        required=True,
        can_skip=False,
        file_type="script",
        file_kind="script",
        role="generic_script",
        inputs=["input_file"],
        outputs=["summary"],
        language="python",
        runtime="python",
        entrypoint=path,
    )


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


def test_validator_unavailable_does_not_block_package():
    error = (
        "E2E_REPAIR_TARGET=__validator__\n"
        "E2E_LAYER=validator_unavailable\n"
        "E2E_STRUCTURED_FAILURE={\"target_file\":\"__validator__\",\"layer\":\"validator_unavailable\"}\n"
        "validator unavailable"
    )
    blocking, warnings = _split_e2e_blocking_errors([error])
    assert blocking == []
    assert warnings and warnings[0]["code"] == "validator_unavailable"


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


def test_single_script_gets_full_coverage_contract_without_io_pollution():
    spec = _script_spec()
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
    assert not any(str(item).startswith("coverage:") for item in spec.inputs)
    assert not any(str(item).startswith("covered:") for item in spec.outputs)
    assert any(item["code"] == "single_script_full_coverage_contract" for item in warnings)


def test_coverage_normalization_does_not_add_bridge_script():
    files = [_script_spec("scripts/parse_document.py", "parse uploaded file"), _script_spec("scripts/render_output.py", "render markdown")]
    warnings = []
    _normalize_file_plan_for_requirement_coverage(
        blueprint_text="支持多种上传格式，解析、清洗、摘要并输出 JSON Markdown。",
        review_summary=PreparePlanReviewSummary(input="uploaded variants", output="JSON Markdown", workflow=["parse", "clean", "summarize", "render"]),
        files_out=files,
        final_outputs=["markdown", "json"],
        warnings=warnings,
    )
    assert all(item.path != "scripts/cover_declared_requirements.py" for item in files)
    assert not any(str(value).startswith(("coverage:", "covered:")) for spec in files for value in [*spec.inputs, *spec.outputs])


def test_declared_pdf_support_not_supported_branch_is_blocking():
    from backend.services.creator.api import _not_supported_declared_input_issue

    source = """
def parse(path):
    if path.lower().endswith('.pdf'):
        raise ValueError('PDF parsing not supported in current implementation')
    return 'ok'
"""
    issue = _not_supported_declared_input_issue(
        source=source,
        blueprint_text='支持 PDF/DOCX/TXT 上传并解析',
        skill_plan_entry={'purpose': 'parse PDF DOCX TXT inputs'},
        file_path='scripts/parse.py',
    )
    assert issue is not None
    assert issue['id'] == 'script_declared_input_not_supported'
    assert 'standard library' in issue['minimal_edit']
