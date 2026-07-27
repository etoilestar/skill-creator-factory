import json
from pathlib import Path

from backend.services.creator import e2e


def _failure(*, step, layer, code, source, exception="FileNotFoundError"):
    return "E2E_STRUCTURED_FAILURE=" + json.dumps({
        "failed_step_index": step,
        "target_file": "scripts/x.py",
        "layer": layer,
        "failed_command": "python scripts/x.py '{}'",
        "stderr": f'Traceback (most recent call last):\n  File "scripts/x.py", line 8, in run\n    {source}\n{exception}: failure',
        "details": {"failure_code": code},
    })


def test_reviewer_prompt_uses_executed_capability_and_semantic_dependency():
    source = Path("backend/services/creator/repair.py").read_text(encoding="utf-8")
    assert "Producing text for a downstream capability remains text generation" in source
    assert "returning an empty placeholder" in source
    assert "Reading or loading a declared dependency alone does not prove" in source
    assert "assigning it to an unused variable" in source


def test_repair_prompt_requires_provenance_and_allows_optional_omission():
    source = Path("backend/services/creator/e2e.py").read_text(encoding="utf-8")
    assert "script-local optional default should preferably be omitted" in source
    assert "A literal appearing only in the failing SKILL.md command is not provenance" in source
    assert "Do not invent options.*, fields.*, payload.*, or config.*" in source
    assert "The previous patch was rejected because it did not materially change" in source


def test_step_advance_within_same_interface_boundary_is_not_progress():
    before = _failure(step=0, layer="external_input_missing", code="external_input_missing", source="resolve_placeholder()", exception="KeyError")
    after = _failure(step=1, layer="argv_schema_error", code="argv_schema_error", source="strict_json_argv_guard()", exception="ValueError")
    assert e2e._e2e_candidate_improved([before], [after], target_file="scripts/x.py") is False


def test_same_file_error_and_function_with_only_expression_change_is_not_progress():
    before = _failure(step=3, layer="script_exit", code="script_exit", source='open("references/a.md")')
    after = _failure(step=3, layer="script_exit", code="script_exit", source='open("../references/a.md")')
    assert e2e._e2e_candidate_improved([before], [after], target_file="scripts/x.py") is False
