"""Regression tests for the 6-point fix set.

Covers:
1. Secondary explore trigger: stub + missing-capability responsibility failures both trigger once.
2. file_specs_for_explore: uses full canonical entry (not a stub-only minimal spec).
3. E2E hard freeze: allow_tool_explore=False on E2E scope; parser rejects tool_pool_patch.add_tool_requests.
4. Public function name: extract_missing_stdlib_from_e2e_errors is importable without leading underscore.
5. runtime_import_guard fail-closed: no binding → all runtime tool imports forbidden.
6. Tool-pool generation and gate: initial pool gate produces allow/deny events.
"""
import json


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
# Fix 5: runtime_import_guard fail-closed (no binding → deny runtime helpers)
# ---------------------------------------------------------------------------

def test_import_guard_no_binding_denies_runtime_helper():
    """With file_binding=None, importing any runtime_tools helper must be denied."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "from backend.services.runtime_tools import read_file_text\n"
    result = guard_runtime_imports(src, "scripts/a.py", None)
    assert not result.success
    assert "read_file_text" in result.forbidden_imports


def test_import_guard_no_binding_allows_stdlib_only():
    """With file_binding=None, code that only uses stdlib must still pass."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "import os\nimport json\n\ndef main():\n    return json.dumps({})\n"
    result = guard_runtime_imports(src, "scripts/a.py", None)
    assert result.success


def test_import_guard_empty_binding_denies_unbound_helper():
    """An explicit empty binding must also deny unbound helpers."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "from backend.services.runtime_tools import extract_pdf_text\n"
    result = guard_runtime_imports(src, "scripts/a.py", {"allowed_helper_imports": []})
    assert not result.success
    assert "extract_pdf_text" in result.forbidden_imports


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

def test_has_responsibility_missing_capability_issue_detects_tool_keywords():
    """Issues mentioning 'tool' / 'helper' / 'capability' / 'dependency' in
    missing_evidence or semantic_failure should trigger exploration."""
    from backend.services.creator.api import _has_responsibility_missing_capability_issue

    issues_with_tool = [
        {
            "id": "some_responsibility_issue",
            "missing_evidence": ["valid runtime helper allowed by selected_tools"],
        }
    ]
    assert _has_responsibility_missing_capability_issue(issues_with_tool) is True

    issues_with_capability = [
        {
            "id": "other_issue",
            "semantic_failure": "missing capability to process PDF files",
        }
    ]
    assert _has_responsibility_missing_capability_issue(issues_with_capability) is True


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

def test_build_tool_pool_produces_gate_events():
    """build_tool_pool must produce at least one gate event for a scripts/ file."""
    from backend.services.creator.tool_pool_builder import build_tool_pool

    pool = build_tool_pool(
        skill_name="x",
        user_request="提取 PDF 文本",
        file_specs=[{"path": "scripts/extract.py", "role": "generic_script", "inputs": ["file.pdf"]}],
    )
    assert pool.file_bindings, "Expected at least one file binding"
    assert any(
        b.target_file == "scripts/extract.py" for b in pool.file_bindings
    ), "Expected binding for scripts/extract.py"


def test_build_tool_pool_gate_blocks_non_script_path():
    """gate_tool_request must block tools for non-scripts/ paths."""
    from backend.services.creator.tool_pool_gate import gate_tool_request

    event = gate_tool_request(
        {"target_file": "references/doc.md", "candidate_tool_id": "unified_file_text_read"},
        file_role="reference",
    )
    assert event.decision == "blocked_by_policy"


# ---------------------------------------------------------------------------
# Fix 6b: Script importing unauthorized helper is rejected by import guard
# ---------------------------------------------------------------------------

def test_import_guard_rejects_unregistered_runtime_helper():
    """A script importing a helper not in allowed_helper_imports must fail guard."""
    from backend.services.creator.runtime_import_guard import guard_runtime_imports

    src = "from backend.services.runtime_tools import extract_pdf_text\n"
    # allowed list does NOT include extract_pdf_text
    result = guard_runtime_imports(
        src,
        "scripts/test.py",
        {"allowed_helper_imports": ["read_file_text"]},
    )
    assert not result.success
    assert "extract_pdf_text" in result.forbidden_imports


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
