"""Regression tests for the 6-point fix set.

Covers:
1. Secondary explore trigger: stub + missing-capability responsibility failures both trigger once.
2. file_specs_for_explore: uses full canonical entry (not a stub-only minimal spec).
3. E2E hard freeze: allow_tool_explore=False on E2E scope; parser rejects tool_pool_patch.add_tool_requests.
4. Public function name: extract_missing_stdlib_from_e2e_errors is importable without leading underscore.
5. runtime_import_guard diagnostics: no binding reports unbound runtime tool imports.
6. Tool-pool generation and gate: initial pool gate produces allow/deny events.
"""
import json

import pytest


# ---------------------------------------------------------------------------
# Fix 4: public function name for extract_missing_stdlib_from_e2e_errors
# ---------------------------------------------------------------------------

def test_extract_missing_stdlib_is_public():
    """The function must be importable as a public name (no leading underscore)."""
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors
    assert callable(extract_missing_stdlib_from_e2e_errors)


def test_extract_missing_stdlib_parses_module_not_found_error():
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors

    errors = [
        "ModuleNotFoundError: No module named 'pillow'\nTraceback ...",
        "ImportError: No module named numpy\nTraceback ...",
    ]
    reqs = extract_missing_stdlib_from_e2e_errors(errors)
    pkgs = [r["package"] for r in reqs]
    assert "pillow" in pkgs
    assert "numpy" in pkgs
    assert all(r["source"] == "e2e_missing_stdlib" for r in reqs)


def test_extract_missing_stdlib_deduplicates():
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors

    errors = [
        "ModuleNotFoundError: No module named 'requests'",
        "ModuleNotFoundError: No module named 'requests'",
    ]
    reqs = extract_missing_stdlib_from_e2e_errors(errors)
    assert len([r for r in reqs if r["package"] == "requests"]) == 1


def test_extract_missing_stdlib_empty_input():
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors

    assert extract_missing_stdlib_from_e2e_errors([]) == []
    assert extract_missing_stdlib_from_e2e_errors(["no error here"]) == []


def test_extract_missing_stdlib_filters_out_python_stdlib_modules():
    """Python stdlib modules (os, sys, json, re …) must NOT be surfaced as pip requests."""
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors

    # These are all Python standard-library modules that cannot be pip-installed.
    stdlib_errors = [
        "ModuleNotFoundError: No module named 'os'",
        "ModuleNotFoundError: No module named 'sys'",
        "ModuleNotFoundError: No module named 'json'",
        "ImportError: No module named re",
        "ModuleNotFoundError: No module named 'pathlib'",
    ]
    reqs = extract_missing_stdlib_from_e2e_errors(stdlib_errors)
    stdlib_pkgs = {r["package"] for r in reqs}
    for stdlib_mod in ("os", "sys", "json", "re", "pathlib"):
        assert stdlib_mod not in stdlib_pkgs, (
            f"stdlib module '{stdlib_mod}' must not be surfaced as a pip install request"
        )


def test_extract_missing_stdlib_third_party_still_surfaced():
    """Third-party packages must still be surfaced even when mixed with stdlib modules."""
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors

    errors = [
        "ModuleNotFoundError: No module named 'requests'",
        "ModuleNotFoundError: No module named 'os'",   # stdlib – must be filtered
        "ModuleNotFoundError: No module named 'pandas'",
    ]
    reqs = extract_missing_stdlib_from_e2e_errors(errors)
    pkgs = {r["package"] for r in reqs}
    assert "requests" in pkgs
    assert "pandas" in pkgs
    assert "os" not in pkgs


# ---------------------------------------------------------------------------
# Fix 5: runtime_import_guard uses available_tools as the import authority
# ---------------------------------------------------------------------------

def test_import_guard_no_binding_reports_runtime_helper():
    """With file_binding=None, importing a runtime_tools helper is rejected as outside available_tools."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "from backend.services.runtime_tools import read_file_text\n"
    result = guard_runtime_imports(src, "scripts/a.py", None)
    assert not result.success
    assert result.error_type == "generated_tool_import_not_in_available_tools"
    assert "backend.services.runtime_tools.read_file_text" in result.forbidden_imports


def test_import_guard_no_binding_allows_stdlib_only():
    """With file_binding=None, code that only uses stdlib must still pass."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "import os\nimport json\n\ndef main():\n    return json.dumps({})\n"
    result = guard_runtime_imports(src, "scripts/a.py", None)
    assert result.success


def test_import_guard_empty_available_tools_rejects_unbound_helper():
    """An explicit empty available_tools pool rejects unbound helpers."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "from backend.services.runtime_tools import extract_pdf_text\n"
    result = guard_runtime_imports(src, "scripts/a.py", {"available_tools": []})
    assert not result.success
    assert result.error_type == "generated_tool_import_not_in_available_tools"
    assert "backend.services.runtime_tools.extract_pdf_text" in result.forbidden_imports


# ---------------------------------------------------------------------------
# Fix 3: E2E hard freeze — CreatorRepairScope.allow_tool_explore=False
# ---------------------------------------------------------------------------

def test_e2e_repair_scope_has_allow_tool_explore_field():
    """CreatorRepairScope must expose allow_tool_explore."""
    from backend.services.creator.repair import CreatorRepairScope

    scope = CreatorRepairScope(
        phase="workflow_e2e",
        repair_type="cross_step_io_alignment",
        target_file="SKILL.md",
        allow_tool_explore=False,
    )
    assert scope.allow_tool_explore is False
    prompt = scope.to_prompt_dict()
    assert prompt["allow_tool_explore"] is False


def test_e2e_repair_scope_defaults_allow_tool_explore_true():
    """Default value of allow_tool_explore must be True (non-E2E phases)."""
    from backend.services.creator.repair import CreatorRepairScope

    scope = CreatorRepairScope(
        phase="module_functional_smoke",
        repair_type="localized_patch",
        target_file="scripts/a.py",
    )
    assert scope.allow_tool_explore is True


def test_parser_rejects_tool_pool_patch_when_frozen():
    """_extract_json_or_diff_proposal must raise when allow_tool_explore=False
    and the model response includes tool_pool_patch.add_tool_requests."""
    from backend.services.creator.repair import _extract_json_or_diff_proposal
    import pytest

    response = json.dumps({
        "target_file": "scripts/a.py",
        "reason": "fix import",
        "edits": [{"old": "old line", "new": "new line"}],
        "tool_pool_patch": {
            "add_tool_requests": [
                {"candidate_tool_id": "some_tool", "target_file": "scripts/a.py"}
            ]
        },
    })
    with pytest.raises(ValueError, match="allow_tool_explore=False"):
        _extract_json_or_diff_proposal(
            response,
            expected_target_file="scripts/a.py",
            allow_tool_explore=False,
        )


def test_parser_allows_tool_pool_patch_when_not_frozen():
    """When allow_tool_explore=True the parser must ignore tool_pool_patch and parse edits."""
    from backend.services.creator.repair import _extract_json_or_diff_proposal

    response = json.dumps({
        "target_file": "scripts/a.py",
        "reason": "fix import",
        "edits": [{"old": "old line", "new": "new line"}],
        "tool_pool_patch": {
            "add_tool_requests": [
                {"candidate_tool_id": "some_tool", "target_file": "scripts/a.py"}
            ]
        },
    })
    proposal = _extract_json_or_diff_proposal(
        response,
        expected_target_file="scripts/a.py",
        allow_tool_explore=True,
    )
    assert proposal.target_file == "scripts/a.py"
    assert proposal.edits


def test_parser_ignores_empty_tool_pool_patch_when_frozen():
    """Empty add_tool_requests list must not trigger the frozen-scope rejection."""
    from backend.services.creator.repair import _extract_json_or_diff_proposal

    response = json.dumps({
        "target_file": "scripts/a.py",
        "reason": "fix",
        "edits": [{"old": "x", "new": "y"}],
        "tool_pool_patch": {"add_tool_requests": []},
    })
    proposal = _extract_json_or_diff_proposal(
        response,
        expected_target_file="scripts/a.py",
        allow_tool_explore=False,
    )
    assert proposal.target_file == "scripts/a.py"


# ---------------------------------------------------------------------------
# Fix 1: Secondary explore trigger — missing capability also triggers once
# ---------------------------------------------------------------------------

def test_has_responsibility_missing_capability_issue_detects_model_tool_support_id():
    """Only explicit model/tool-support issue IDs should trigger exploration;
    Backend should not infer capabilities from helper/capability words."""
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    issues_with_support_id = [{"id": "tool_support_insufficient", "semantic_failure": "model judged current ToolPool insufficient"}]
    assert _has_responsibility_missing_capability_issue(issues_with_support_id) is True

    keyword_only = [{"id": "some_responsibility_issue", "missing_evidence": ["valid runtime helper allowed by selected_tools"]}]
    assert _has_responsibility_missing_capability_issue(keyword_only) is False


def test_has_responsibility_missing_capability_issue_ignores_unrelated():
    """Issues not mentioning tool/helper/capability/dependency must return False."""
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    issues = [
        {
            "id": "stub_implementation",
            "semantic_failure": "function body only has pass statement",
        }
    ]
    assert _has_responsibility_missing_capability_issue(issues) is False


def test_has_responsibility_missing_capability_issue_detects_explicit_id():
    """Explicit tool-binding IDs must also return True."""
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    for issue_id in {
        "responsibility_tool_binding_failed",
        "missing_tool_binding",
        "missing_required_tool",
    }:
        issues = [{"id": issue_id, "semantic_failure": "some failure"}]
        assert _has_responsibility_missing_capability_issue(issues) is True, f"failed for id={issue_id!r}"


def test_has_responsibility_missing_capability_issue_empty():
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    assert _has_responsibility_missing_capability_issue([]) is False
    assert _has_responsibility_missing_capability_issue(None) is False


# ---------------------------------------------------------------------------
# Fix 6a: Initial tool-pool generation and gate
# ---------------------------------------------------------------------------

def test_build_tool_pool_does_not_require_keyword_driven_binding():
    """ToolPool generation should not rely on backend PDF/text keyword mappings."""
    from backend.services.creator.tool_pool_builder import build_tool_pool

    pool = build_tool_pool(
        skill_name="x",
        user_request="提取 PDF 文本",
        file_specs=[{"path": "scripts/extract.py", "role": "generic_script", "inputs": ["file.pdf"]}],
    )
    assert pool.skill_name == "x"
    assert isinstance(pool.file_bindings, list)


def test_build_tool_pool_gate_is_file_role_independent():
    """gate_tool_request must not use file_role as a tool semantic decision."""
    from backend.services.creator.tool_pool_gate import gate_tool_request

    event = gate_tool_request(
        {"target_file": "references/doc.md", "candidate_tool_id": "unified_file_text_read"},
        file_role="reference",
    )
    assert event.decision == "allow"


# ---------------------------------------------------------------------------
# Fix 6b: Script importing a helper outside available_tools is rejected by import guard
# ---------------------------------------------------------------------------

def test_import_guard_rejects_helper_outside_available_tools():
    """A script importing a helper not in available_tools is rejected deterministically."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "from backend.services.runtime_tools import extract_pdf_text\n"
    # available_tools does NOT include extract_pdf_text
    result = guard_runtime_imports(
        src,
        "scripts/test.py",
        {
            "available_tools": [{
                "tool_id": "file_reader",
                "function_name": "read_file_text",
                "import_path": "backend.services.runtime_tools",
                "input_schema": {},
                "output_schema": {},
            }]
        },
    )
    assert not result.success
    assert result.error_type == "generated_tool_import_not_in_available_tools"
    assert "backend.services.runtime_tools.extract_pdf_text" in result.forbidden_imports


# ---------------------------------------------------------------------------
# Fix 6c: E2E phase cannot expand the tool pool (structured request surfaced instead)
# ---------------------------------------------------------------------------

def test_e2e_missing_stdlib_surfaced_not_tool_exploration():
    """E2E missing stdlib package → structured missing_stdlib_request, not tool exploration."""
    from backend.services.creator.e2e import extract_missing_stdlib_from_e2e_errors

    e2e_error = (
        "E2E_REPAIR_TARGET=scripts/run.py\n"
        "E2E_LAYER=script_exit\n"
        "return_code=1\n"
        "ModuleNotFoundError: No module named 'pandas'\n"
        "Traceback (most recent call last):\n"
        "  File 'scripts/run.py', line 2, in <module>\n"
        "    import pandas\n"
    )
    reqs = extract_missing_stdlib_from_e2e_errors([e2e_error])
    assert reqs, "Expected at least one stdlib request"
    assert reqs[0]["package"] == "pandas"
    assert reqs[0]["source"] == "e2e_missing_stdlib"


@pytest.mark.asyncio
async def test_tool_support_insufficient_recall_candidates_still_pass_backend_gate(monkeypatch, tmp_path):
    """Supplemental recall proposals must flow through gate_tool_request before ToolPool writes."""
    from backend.services.creator import api
    from backend.services.creator.tool_pool_store import load_tool_pool

    monkeypatch.setattr(api.settings, "skills_path", tmp_path)

    def fake_recall(*args, **kwargs):
        return (
            [
                {"tool_id": "system_text_generation", "recalled_for_capabilities": ["structured_id"]},
                {"tool_id": "definitely_not_registered_tool", "recalled_for_capabilities": ["structured_id"]},
            ],
            "test_recall_union",
        )

    async def fake_complete(messages, model):
        return json.dumps({
            "gap_type": "capability_gap",
            "reason": "structured issue requested missing tool support",
            "tool_pool_patch": {
                "add_tool_requests": [
                    {"candidate_tool_id": "system_text_generation", "requested_capability": "text"},
                    {"candidate_tool_id": "definitely_not_registered_tool", "requested_capability": "missing"},
                ],
                "remove_tool_requests": [],
                "reason": "candidate recall must still use Backend Gate",
                "affected_files": ["scripts/a.py"],
            },
        })

    monkeypatch.setattr(api, "_recall_creator_tool_candidates", fake_recall)
    monkeypatch.setattr(api, "complete_chat_once", fake_complete)

    result = await api._plan_tool_pool_patch_from_responsibility_feedback(
        skill_name="demo",
        target_file="scripts/a.py",
        file_spec={"path": "scripts/a.py", "required": True},
        responsibility_issues=[{"id": "tool_support_insufficient"}],
        script_content="def run(args):\n    return {'text': ''}\n",
        requested_model=None,
    )

    pool = load_tool_pool(tmp_path / "demo")
    allowed_ids = {tool.tool_id for tool in pool.tools if tool.status == "allowed"}
    denied_ids = {item.tool_id for item in pool.denied_requests}
    gate_ids = {event.tool_id for event in pool.gate_events}

    assert result["allowed_new"] == 1
    assert result["denied_new"] == 1
    assert "system_text_generation" in allowed_ids
    assert "definitely_not_registered_tool" not in allowed_ids
    assert "definitely_not_registered_tool" in denied_ids
    assert {"system_text_generation", "definitely_not_registered_tool"} <= gate_ids


def test_tool_contract_mismatch_does_not_trigger_missing_capability_recall():
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    assert _has_responsibility_missing_capability_issue([
        {"id": "tool_contract_mismatch", "reason": "platform import not in current ToolPool"}
    ]) is False


def test_plain_responsibility_issue_with_real_function_name_does_not_trigger_recall():
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    assert _has_responsibility_missing_capability_issue([
        {
            "id": "script_functional.responsibility",
            "reason": "The implementation mentions read_file_text but still returns a fixed string.",
            "minimal_edit": "Use the current inputs to produce a non-empty output.",
        }
    ]) is False


def test_supplemental_tool_pool_patch_source_keeps_gate_as_authorization_boundary():
    import inspect
    from backend.services.creator import api

    source = inspect.getsource(api._apply_planner_tool_pool_patch)
    gate_index = source.index("gate_tool_request(")
    append_index = source.index("pool.tools.append(")
    assert gate_index < append_index
    assert "if gate_event.decision == \"allow\":" in source


def test_first_round_responsibility_recall_trigger_requires_structured_missing_tool_id():
    import inspect
    from backend.services.creator import api

    source = inspect.getsource(api.generate_file)
    assert "_has_responsibility_missing_capability_issue(" in source
    assert "_plan_tool_pool_patch_from_responsibility_feedback(" in source


def test_second_round_e2e_does_not_recall_or_modify_toolpool():
    import inspect
    from backend.services.creator import e2e

    source = inspect.getsource(e2e)
    assert "_plan_tool_pool_patch_from_responsibility_feedback" not in source
    assert "_recall_creator_tool_candidates" not in source
    assert "gate_tool_request(" not in source
    assert "save_tool_pool(" not in source
