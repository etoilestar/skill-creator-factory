"""Tests for pure helper/parser functions in backend/routers/chat.py.

These functions do not require a running LLM or file system access.
"""

import json
import pytest


# ---------------------------------------------------------------------------
# _strip_markdown_json_fence
# ---------------------------------------------------------------------------

def test_strip_plain_json():
    from backend.routers.chat import _strip_markdown_json_fence

    assert _strip_markdown_json_fence('{"a": 1}') == '{"a": 1}'


def test_strip_json_fence():
    from backend.routers.chat import _strip_markdown_json_fence

    text = '```json\n{"a": 1}\n```'
    assert _strip_markdown_json_fence(text) == '{"a": 1}'


def test_strip_bare_fence_with_json():
    from backend.routers.chat import _strip_markdown_json_fence

    text = '```\n{"a": 1}\n```'
    assert _strip_markdown_json_fence(text).startswith("{")


def test_strip_embedded_json_block():
    from backend.routers.chat import _strip_markdown_json_fence

    text = "Here is the result:\n```json\n{\"ok\": true}\n```\nDone."
    result = _strip_markdown_json_fence(text)
    assert result.startswith("{")
    assert '"ok"' in result


def test_strip_fallback_finds_json_in_prose():
    from backend.routers.chat import _strip_markdown_json_fence

    text = 'Sure! Here is the JSON: {"result": 42} — done.'
    result = _strip_markdown_json_fence(text)
    assert '{"result": 42}' in result


# ---------------------------------------------------------------------------
# _parse_need_body_decision
# ---------------------------------------------------------------------------

def test_parse_need_body_true():
    from backend.routers.chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"need_body": true}') is True


def test_parse_need_body_false():
    from backend.routers.chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"need_body": false}') is False


def test_parse_need_body_string_true():
    from backend.routers.chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"need_body": "true"}') is True


def test_parse_need_body_invalid_json_defaults_true():
    """Invalid JSON should default to True (safe: load body anyway)."""
    from backend.routers.chat import _parse_need_body_decision

    assert _parse_need_body_decision("not json at all") is True


def test_parse_need_body_missing_key_defaults_true():
    from backend.routers.chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"reason": "irrelevant"}') is True


# ---------------------------------------------------------------------------
# _parse_child_skill_decision
# ---------------------------------------------------------------------------

def test_parse_child_skill_need_child_true():
    from backend.routers.chat import _parse_child_skill_decision

    text = '{"need_child": true, "child_ref": "skills/child-a", "reason": "matched"}'
    result = _parse_child_skill_decision(
        text, valid_child_refs={"skills/child-a", "skills/child-b"}
    )
    assert result["need_child"] is True
    assert result["child_ref"] == "skills/child-a"


def test_parse_child_skill_invalid_ref_rejected():
    from backend.routers.chat import _parse_child_skill_decision

    text = '{"need_child": true, "child_ref": "skills/hacked", "reason": ""}'
    result = _parse_child_skill_decision(
        text, valid_child_refs={"skills/real-child"}
    )
    assert result["need_child"] is False


def test_parse_child_skill_no_need_child():
    from backend.routers.chat import _parse_child_skill_decision

    text = '{"need_child": false, "reason": "not matched"}'
    result = _parse_child_skill_decision(text, valid_child_refs={"skills/x"})
    assert result["need_child"] is False
    assert result["child_ref"] == ""


def test_parse_child_skill_invalid_json():
    from backend.routers.chat import _parse_child_skill_decision

    result = _parse_child_skill_decision("NOT JSON", valid_child_refs=set())
    assert result["need_child"] is False


def test_parse_child_skill_missing_child_ref():
    from backend.routers.chat import _parse_child_skill_decision

    text = '{"need_child": true, "reason": "match"}'
    result = _parse_child_skill_decision(text, valid_child_refs={"skills/x"})
    assert result["need_child"] is False
    assert "child_ref" in result["reason"]


# ---------------------------------------------------------------------------
# _parse_resource_selection_decision
# ---------------------------------------------------------------------------

def _make_catalog(n: int = 3) -> list[dict]:
    return [
        {
            "resource_handle": f"resource:{i}",
            "path": f"references/ref{i}.md",
            "kind": "reference",
            "title": f"Ref {i}",
            "allowed_actions": ["read_resource"],
        }
        for i in range(n)
    ]


def test_parse_resource_selection_picks_valid_handles():
    from backend.routers.chat import _parse_resource_selection_decision

    catalog = _make_catalog(3)
    text = '{"need_resources": true, "resource_handles": ["resource:0", "resource:2"], "reason": "need"}'
    result = _parse_resource_selection_decision(text, resource_catalog=catalog)

    assert result["need_resources"] is True
    assert "resource:0" in result["resource_handles"]
    assert "resource:2" in result["resource_handles"]


def test_parse_resource_selection_ignores_invalid_handles():
    from backend.routers.chat import _parse_resource_selection_decision

    catalog = _make_catalog(2)
    text = '{"need_resources": true, "resource_handles": ["resource:99"], "reason": "oops"}'
    result = _parse_resource_selection_decision(text, resource_catalog=catalog)

    # resource:99 is not in catalog — selection should be empty, need_resources → False
    assert result["need_resources"] is False
    assert result["resource_handles"] == []


def test_parse_resource_selection_caps_at_five():
    from backend.routers.chat import _parse_resource_selection_decision

    catalog = _make_catalog(10)
    handles = [f"resource:{i}" for i in range(10)]
    text = json.dumps({"need_resources": True, "resource_handles": handles, "reason": "all"})
    result = _parse_resource_selection_decision(text, resource_catalog=catalog)

    assert len(result["resource_handles"]) <= 5


def test_parse_resource_selection_invalid_json():
    from backend.routers.chat import _parse_resource_selection_decision

    result = _parse_resource_selection_decision("bad json", resource_catalog=[])
    assert result["need_resources"] is False


# ---------------------------------------------------------------------------
# _planner_model_name
# ---------------------------------------------------------------------------

def test_planner_model_name_falls_back_to_default():
    from backend.routers.chat import _planner_model_name
    from backend.config import settings
    from unittest.mock import patch

    with patch.object(settings, "planner_model", None):
        assert _planner_model_name("my-default") == "my-default"


def test_planner_model_name_uses_configured():
    from backend.routers.chat import _planner_model_name
    from backend.config import settings
    from unittest.mock import patch

    with patch.object(settings, "planner_model", "fast-model"):
        assert _planner_model_name("other") == "fast-model"


# ---------------------------------------------------------------------------
# _normalize_skill_runtime_plan
# ---------------------------------------------------------------------------

def test_normalize_plan_raises_on_non_dict():
    from backend.routers.chat import _normalize_skill_runtime_plan

    with pytest.raises(ValueError):
        _normalize_skill_runtime_plan("not a dict")


def test_normalize_plan_direct_answer_mode():
    from backend.routers.chat import _normalize_skill_runtime_plan

    plan = {"mode": "direct_answer", "actions": [], "errors": [], "missing": []}
    result = _normalize_skill_runtime_plan(plan)
    assert result["mode"] == "direct_answer"
    assert result["tasks"] == []


def test_normalize_plan_unknown_mode_becomes_ask_user():
    from backend.routers.chat import _normalize_skill_runtime_plan

    plan = {"mode": "invalid_mode", "actions": [], "errors": [], "missing": []}
    result = _normalize_skill_runtime_plan(plan)
    assert result["mode"] == "ask_user"


def test_normalize_plan_read_resource_without_handle():
    from backend.routers.chat import _normalize_skill_runtime_plan

    plan = {
        "mode": "execute",
        "actions": [{"action": "read_resource"}],
        "errors": [],
        "missing": [],
    }
    result = _normalize_skill_runtime_plan(plan, resource_catalog=[])
    # The action should be rejected and moved to errors
    assert any("read_resource" in str(e) for e in result["errors"])
    assert result["tasks"] == []


def test_normalize_plan_execute_with_all_actions_rejected_becomes_ask_user():
    from backend.routers.chat import _normalize_skill_runtime_plan

    plan = {
        "mode": "execute",
        "actions": [
            {"action": "run_command", "command": "nonexistent_binary_xyz arg1"},
        ],
        "errors": [],
        "missing": [],
    }
    result = _normalize_skill_runtime_plan(plan, resource_catalog=[])
    # run_command precheck will fail (binary doesn't exist), errors populated
    assert result["mode"] == "ask_user"


# ---------------------------------------------------------------------------
# _validate_skill_md
# ---------------------------------------------------------------------------

def test_validate_skill_md_valid(tmp_path):
    from backend.routers.chat import _validate_skill_md

    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: my-skill\ndescription: test\n---\n# Body\n")
    _validate_skill_md(md)  # should not raise


def test_validate_skill_md_missing_file(tmp_path):
    from backend.routers.chat import _validate_skill_md

    with pytest.raises(ValueError, match="SKILL.md"):
        _validate_skill_md(tmp_path / "SKILL.md")


def test_validate_skill_md_no_frontmatter(tmp_path):
    from backend.routers.chat import _validate_skill_md

    md = tmp_path / "SKILL.md"
    md.write_text("# No frontmatter here\n")
    with pytest.raises(ValueError, match="frontmatter"):
        _validate_skill_md(md)


def test_validate_skill_md_missing_name(tmp_path):
    from backend.routers.chat import _validate_skill_md

    md = tmp_path / "SKILL.md"
    md.write_text("---\ndescription: no name field\n---\n# Body\n")
    with pytest.raises(ValueError, match="name"):
        _validate_skill_md(md)


def test_validate_skill_md_name_too_long(tmp_path):
    from backend.routers.chat import _validate_skill_md

    md = tmp_path / "SKILL.md"
    long_name = "a" * 65
    md.write_text(f"---\nname: {long_name}\ndescription: ok\n---\n# Body\n")
    with pytest.raises(ValueError, match="64"):
        _validate_skill_md(md)


# ---------------------------------------------------------------------------
# _extract_all_fenced_blocks
# ---------------------------------------------------------------------------

def test_extract_fenced_blocks_basic():
    from backend.routers.chat import _extract_all_fenced_blocks

    text = "Before\n```python\nprint('hello')\n```\nAfter"
    blocks = _extract_all_fenced_blocks(text)

    assert len(blocks) == 1
    assert blocks[0].lang == "python"
    assert "print" in blocks[0].code


def test_extract_multiple_blocks():
    from backend.routers.chat import _extract_all_fenced_blocks

    text = "```bash\necho hi\n```\n\n```python\nprint('world')\n```"
    blocks = _extract_all_fenced_blocks(text)

    assert len(blocks) == 2
    assert blocks[0].lang == "bash"
    assert blocks[1].lang == "python"


def test_extract_unclosed_block_ignored():
    from backend.routers.chat import _extract_all_fenced_blocks

    text = "```python\nprint('oops')"
    blocks = _extract_all_fenced_blocks(text)

    assert blocks == []


def test_extract_blocks_no_blocks():
    from backend.routers.chat import _extract_all_fenced_blocks

    blocks = _extract_all_fenced_blocks("Just plain text.")
    assert blocks == []
