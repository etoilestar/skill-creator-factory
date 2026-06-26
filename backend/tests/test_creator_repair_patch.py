import pytest

from backend.services.creator import repair
from backend.services.creator.repair import (
    CreatorDiffProposal,
    CreatorRepairProposalParseError,
    CreatorRepairScope,
    _apply_exact_replace_patch,
    _apply_deterministic_micro_patch_if_safe,
    _apply_unified_diff_or_convert_to_exact,
    _extract_json_or_diff_proposal,
    _validate_repair_diff_scope,
    _request_and_apply_repair_patch,
)


def _proposal(target_file="SKILL.md", old="", new=""):
    return CreatorDiffProposal(target_file=target_file, reason="test", edits=[{"old": old, "new": new}])


def test_exact_match_success_uses_original_logic():
    original = "alpha\nbeta\ngamma\n"
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old="beta", new="BETA"),
        expected_target_file="SKILL.md",
    )

    assert candidate == "alpha\nBETA\ngamma\n"
    assert stats["applied"][0]["fallback_type"] == "exact"


def test_normalized_fallback_handles_punctuation_and_whitespace():
    original = "标题： “你好”  \n下一行\n"
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old='标题: "你好" 下一行', new="标题：您好\n下一行"),
        expected_target_file="SKILL.md",
    )

    assert candidate == "标题：您好\n下一行\n"
    assert stats["applied"][0]["fallback_type"] == "normalized_exact"
    assert stats["applied"][0]["similarity"] == 1.0


def test_approximate_substring_fallback_succeeds_for_unique_high_similarity_span():
    original = "# Skill\n\nUse `scripts/run.py` to read input, validate payload, and write the final report.\n"
    old = "Use `scripts/run.py` to read inputs, validate payload, and write final report."
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old=old, new="Use `scripts/run.py` to validate input and write a final report."),
        expected_target_file="SKILL.md",
    )

    assert "validate input and write a final report" in candidate
    assert stats["applied"][0]["fallback_type"] == "approximate_substring"
    assert stats["applied"][0]["similarity"] >= 0.88
    assert "original_model_old_excerpt" in stats["applied"][0]


def test_approximate_substring_rejects_multiple_high_similarity_candidates():
    original = (
        "First block: use the parser to validate payload and write the final report.\n"
        "Second block: use the parser to validate payload and write the final report.\n"
    )
    old = "use the parser to validate payload and write final report"

    with pytest.raises(ValueError, match="approximate substring .*拒绝"):
        _apply_exact_replace_patch(
            original_content=original,
            proposal=_proposal(old=old, new="replacement"),
            expected_target_file="SKILL.md",
        )


def test_scripts_python_does_not_use_approximate_substring():
    original = "def run(payload):\n    return {'result': payload}\n"
    old = "def run(data):\n    return {'result': data}\n"

    with pytest.raises(ValueError, match="不启用 approximate substring"):
        _apply_exact_replace_patch(
            original_content=original,
            proposal=_proposal(target_file="scripts/main.py", old=old, new="def run(payload):\n    return {'ok': payload}\n"),
            expected_target_file="scripts/main.py",
        )


def test_fallback_still_respects_changed_line_count_scope():
    original = "# Skill\n\nUse `scripts/run.py` to read input, validate payload, and write the final report.\n"
    old = "Use `scripts/run.py` to read inputs, validate payload, and write final report."
    proposal = _proposal(old=old, new="line1\nline2\nline3\nline4")
    scope = CreatorRepairScope(phase="test", repair_type="test", target_file="SKILL.md", max_changed_lines=1)

    with pytest.raises(ValueError, match="修改行数超过"):
        _validate_repair_diff_scope(proposal=proposal, current_content=original, scope=scope)


@pytest.mark.asyncio
async def test_repeated_unapplicable_proposal_is_rejected_without_third_retry(monkeypatch):
    calls = 0
    proposal = _proposal(target_file="scripts/main.py", old="missing exact old", new="replacement")

    async def fake_request(**kwargs):
        nonlocal calls
        calls += 1
        return proposal

    monkeypatch.setattr(repair, "_request_repair_diff_proposal", fake_request)

    scope = CreatorRepairScope(phase="test", repair_type="test", target_file="scripts/main.py")
    with pytest.raises(ValueError, match="REPEATED_UNAPPLICABLE_PROPOSAL"):
        await _request_and_apply_repair_patch(
            model="test-model",
            file_path="scripts/main.py",
            current_content="print('hello')\n",
            failure_text="failure",
            scope=scope,
            task_context="ctx",
            target_rule="rule",
            patch_retry_limit=3,
        )

    assert calls == 2


def test_old_lines_new_lines_patch_parses_to_exact_replace():
    proposal = _extract_json_or_diff_proposal(
        '{"target_file":"SKILL.md","edits":[{"old_lines":["alpha","beta"],"new_lines":["alpha","BETA"]}]}',
        expected_target_file="SKILL.md",
    )

    assert proposal.edits == [{"old": "alpha\nbeta", "new": "alpha\nBETA"}]


def test_old_lines_handles_shell_json_argv_without_polluting_target_content():
    proposal = _extract_json_or_diff_proposal(
        """
        {
          "target_file": "SKILL.md",
          "reason": "fix argv",
          "edits": [{
            "old_lines": ["```bash", "python scripts/run.py '{\\"topic\\": \\"{{topic}}\\"}'", "```"],
            "new_lines": ["```bash", "python scripts/run.py '{\\"topic\\": \\"{{topic}}\\", \\"count\\": 3}'", "```"]
          }]
        }
        """,
        expected_target_file="SKILL.md",
    )
    assert proposal.edits[0]["old"] == '```bash\npython scripts/run.py \'{"topic": "{{topic}}"}\'\n```'
    assert "\\{" not in proposal.edits[0]["new"]


def test_malformed_json_with_fenced_diff_is_recovered():
    text = '''{"target_file":"SKILL.md","diff":"bad " quote
```diff
--- a/SKILL.md
+++ b/SKILL.md
@@ -1 +1 @@
-old
+new
```
}'''
    proposal = _extract_json_or_diff_proposal(text, expected_target_file="SKILL.md")
    assert proposal.mode == "unified_diff"
    assert "old" in proposal.diff


def test_parse_failure_is_structured_when_no_patch_schema_found():
    with pytest.raises(CreatorRepairProposalParseError) as exc:
        _extract_json_or_diff_proposal("not a patch", expected_target_file="SKILL.md")
    assert exc.value.diff_extraction_attempted is True
    assert exc.value.last_output_excerpt == "not a patch"


def test_unified_diff_bad_line_number_converts_to_exact_replace_for_markdown():
    original = "one\nold\nthree\n"
    proposal = CreatorDiffProposal(
        target_file="SKILL.md",
        reason="bad hunk line",
        diff="--- a/SKILL.md\n+++ b/SKILL.md\n@@ -99,3 +99,3 @@\n one\n-old\n+new\n three\n",
        mode="unified_diff",
    )
    candidate, stats = _apply_unified_diff_or_convert_to_exact(
        original_content=original,
        proposal=proposal,
        expected_target_file="SKILL.md",
    )
    assert candidate == "one\nnew\nthree\n"
    assert stats["mode"] == "unified_diff_to_exact_replace"


def test_unified_diff_to_exact_does_not_enable_approximate_for_python_scripts():
    original = "def run(payload):\n    return {'result': payload}\n"
    proposal = CreatorDiffProposal(
        target_file="scripts/main.py",
        reason="bad approximate hunk",
        diff=(
            "--- a/scripts/main.py\n+++ b/scripts/main.py\n@@ -50,2 +50,2 @@\n"
            "-def run(data):\n-    return {'result': data}\n+def run(payload):\n+    return {'ok': payload}\n"
        ),
        mode="unified_diff",
    )
    with pytest.raises(ValueError, match="不启用 approximate substring"):
        _apply_unified_diff_or_convert_to_exact(
            original_content=original,
            proposal=proposal,
            expected_target_file="scripts/main.py",
        )


def test_deterministic_micro_patch_uses_structured_repair_ops_only():
    scope = CreatorRepairScope(phase="test", repair_type="test", target_file="SKILL.md")
    failure = {
        "target_file": "SKILL.md",
        "repair_ops": [{
            "op": "append_after",
            "anchor": "Reference rules",
            "text": "\nRead references only when needed.",
        }],
    }
    result = _apply_deterministic_micro_patch_if_safe(
        failures=[failure],
        current_content="# Skill\n\nReference rules\n",
        scope=scope,
    )

    assert result is not None
    _proposal, candidate, stats = result
    assert "Read references only when needed." in candidate
    assert stats["mode"] == "deterministic_micro_patch"


def test_deterministic_micro_patch_ignores_natural_language_minimal_edit():
    scope = CreatorRepairScope(phase="test", repair_type="test", target_file="SKILL.md")
    result = _apply_deterministic_micro_patch_if_safe(
        failures=[{
            "target_file": "SKILL.md",
            "evidence": "Reference rules",
            "minimal_edit": "append a read-only note after the reference section",
        }],
        current_content="# Skill\n\nReference rules\n",
        scope=scope,
    )

    assert result is None


def test_resource_role_conflicts_use_structured_claims_only():
    from backend.services.creator.api import normalize_skill_md_failures

    advisory = normalize_skill_md_failures([{
        "target_file": "SKILL.md",
        "resource_role": "reference",
        "claim_type": "forbid_read",
        "severity": "error",
        "message": "structured read prohibition",
    }])
    hard = normalize_skill_md_failures([{
        "target_file": "SKILL.md",
        "resource_role": "reference",
        "claim_type": "execution_step",
        "severity": "error",
        "message": "structured execution claim",
    }])

    assert advisory == []
    assert hard and "只读" in hard[0]["expected"]


def test_failure_ledger_drops_resolved_previous_failure():
    from backend.services.creator.api import failure_ledger_for_skill_md_finalize

    previous = [{"target_file": "SKILL.md", "layer": "review", "id": "a", "evidence": "old"}]
    current = [{"target_file": "SKILL.md", "layer": "review", "id": "b", "evidence": "new"}]
    ledger = failure_ledger_for_skill_md_finalize(current, previous_remaining=previous)

    assert ledger["resolved_failures"] == previous
    assert all(item["id"] != "a" for item in ledger["remaining_failures"])


def test_normalized_span_mapping_trims_spans_with_surrounding_whitespace():
    original = "\n\n  标题： “你好”  \n下一行\n  "
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old='标题: "你好" 下一行', new="标题：您好\n下一行"),
        expected_target_file="SKILL.md",
    )

    assert candidate == "\n\n  标题：您好\n下一行\n  "
    assert stats["applied"][0]["fallback_type"] == "normalized_exact"


def test_markdown_patch_regression_unclosed_fence_is_rejected():
    original = "---\nname: x\ndescription: y\n---\n\n# Use\n\nText.\n"
    proposal = CreatorDiffProposal(
        target_file="SKILL.md",
        reason="test",
        edits=[{"old": "Text.", "new": "```bash\npython scripts/a.py '{}'"}],
    )
    with pytest.raises(ValueError, match="hard Markdown format regression"):
        _validate_repair_diff_scope(
            proposal=proposal,
            current_content=original,
            scope=CreatorRepairScope(phase="test", repair_type="localized_patch", target_file="SKILL.md"),
        )


def test_command_patch_escaped_json_argv_is_rejected():
    original = "---\nname: x\ndescription: y\n---\n\n```bash\npython scripts/a.py '{\"k\":\"v\"}'\n```\n"
    proposal = CreatorDiffProposal(
        target_file="SKILL.md",
        reason="test",
        edits=[{"old": "python scripts/a.py '{\"k\":\"v\"}'", "new": "python scripts/a.py '{\\\"k\\\":\\\"v\\\"}'"}],
    )
    with pytest.raises(ValueError, match="backslash-escaped shell JSON argv"):
        _validate_repair_diff_scope(
            proposal=proposal,
            current_content=original,
            scope=CreatorRepairScope(phase="test", repair_type="localized_patch", target_file="SKILL.md"),
        )


def test_nonstandard_shell_text_with_backslashes_is_not_rejected_by_argv_guard():
    original = "---\nname: x\ndescription: y\n---\n\n```bash\necho '{\\\"k\\\":\\\"v\\\"}'\n```\n\nText.\n"
    proposal = CreatorDiffProposal(
        target_file="SKILL.md",
        reason="test",
        edits=[{"old": "Text.", "new": "Updated text."}],
    )
    candidate, _stats = _validate_repair_diff_scope(
        proposal=proposal,
        current_content=original,
        scope=CreatorRepairScope(phase="test", repair_type="localized_patch", target_file="SKILL.md"),
    )
    assert "Updated text." in candidate
