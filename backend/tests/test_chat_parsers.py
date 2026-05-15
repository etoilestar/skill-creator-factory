"""Tests for pure helper/parser functions in backend/routers/chat.py.

These functions do not require a running LLM or file system access.
Also includes tests for _is_within_sandbox, a security-critical guard that
rejects symlinks escaping the skill execution sandbox.
"""

import json
import pytest

STATE_A_PROMPT_PREFIX = "好的，我先确认一个关键信息"
BLUEPRINT_MARKER = "Skill 蓝图"


# ---------------------------------------------------------------------------
# creator requirement gating
# ---------------------------------------------------------------------------

def test_analyze_creator_requirements_detects_missing_slots():
    from backend.routers.chat import ChatRequest, Message, _analyze_creator_requirements

    request = ChatRequest(
        messages=[
            Message(role="user", content="帮我做一个写故事的 Skill"),
        ]
    )

    result = _analyze_creator_requirements(request)

    assert result.user_turns == 1
    assert "purpose" in result.collected_slots
    assert "input" in result.missing_slots
    assert "scenario" in result.missing_slots
    assert result.ready_for_blueprint is False
    assert "关键信息" in result.next_question


def test_detect_creator_state_first_turn_full_request_stays_a():
    from backend.routers.chat import ChatRequest, Message, _detect_creator_state

    request = ChatRequest(
        messages=[
            Message(
                role="user",
                content=(
                    "帮我做一个会议纪要整理 Skill。"
                    "输入是会议记录文本，输出是行动项清单。"
                    "典型场景是项目经理会后整理待办。"
                    "不需要脚本或外部服务。"
                ),
            ),
        ]
    )

    result = _detect_creator_state(request)

    assert result.state == "A"
    assert result.requirements.ready_for_blueprint is False


def test_detect_creator_state_ready_for_blueprint_after_second_turn():
    from backend.routers.chat import ChatRequest, Message, _detect_creator_state

    request = ChatRequest(
        messages=[
            Message(role="user", content="帮我做一个写故事的 Skill"),
            Message(
                role="assistant",
                content="好的，我先确认一个关键信息：用户实际会提供什么输入，它最终又应该输出什么结果？最好直接给我一条真实示例。",
            ),
            Message(
                role="user",
                content=(
                    "用户输入故事主题和风格，输出一个短篇故事。"
                    "典型场景是用户会说：请写一个关于太空猫的温馨故事。"
                    "不需要脚本、参考资料或外部依赖。"
                ),
            ),
        ]
    )

    result = _detect_creator_state(request)

    assert result.state == "B"
    assert result.requirements.ready_for_blueprint is True


def test_detect_creator_state_requires_assistant_follow_up_between_user_turns():
    from backend.routers.chat import ChatRequest, Message, _detect_creator_state

    request = ChatRequest(
        messages=[
            Message(role="user", content="帮我做一个写故事的 Skill"),
            Message(
                role="user",
                content=(
                    "用户输入故事主题和风格，输出一个短篇故事。"
                    "典型场景是用户会说：请写一个关于太空猫的温馨故事。"
                    "不需要脚本、参考资料或外部依赖。"
                ),
            ),
            Message(
                role="assistant",
                content="好的，我再确认一个关键细节：如果只能优先保证一项，你更希望优先质量还是速度？",
            ),
        ]
    )

    result = _detect_creator_state(request)

    assert result.state == "A"
    assert result.requirements.ready_for_blueprint is False


@pytest.mark.asyncio
async def test_state_a_returns_clarifying_question_without_llm():
    from backend.routers.chat import ChatRequest, Message, _make_stream

    request = ChatRequest(messages=[Message(role="user", content="帮我做一个写故事的 Skill")])
    skill_context = {
        "skill_name": "skill-creator",
        "metadata_prompt": "",
        "body_loader": lambda: "body",
        "child_body_loader": None,
        "force_body": True,
        "enable_action_execution": True,
        "require_action_confirmation": True,
        "execution_root": None,
        "strict_creator_generation": True,
        "skip_runtime_planner_before_confirmation": True,
        "disable_runtime_planner": True,
        "enable_resource_preload": True,
        "use_frontend_driven_creation": True,
    }

    response = _make_stream(skill_context, request)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

    text = "".join(chunks)
    # State A must return a deterministic clarifying question instead of a blueprint.
    assert STATE_A_PROMPT_PREFIX in text
    assert BLUEPRINT_MARKER not in text


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


def test_strip_multiple_json_objects_picks_first():
    """Bracket-depth scan must stop at the first complete object, not greedily
    match from the first ``{`` to the last ``}``."""
    from backend.routers.chat import _strip_markdown_json_fence

    text = 'Result: {"key": "a"} and also {"key": "b"} done.'
    result = _strip_markdown_json_fence(text)
    assert result == '{"key": "a"}'


def test_strip_nested_json_in_prose():
    """Bracket-depth scan must correctly track depth for nested objects."""
    from backend.routers.chat import _strip_markdown_json_fence

    text = 'Response: {"outer": {"inner": 1}} done.'
    result = _strip_markdown_json_fence(text)
    assert result == '{"outer": {"inner": 1}}'


def test_strip_json_with_string_containing_braces():
    """Brace characters inside string values must not confuse the depth counter."""
    from backend.routers.chat import _strip_markdown_json_fence

    text = 'Hmm: {"template": "use {name} here"} rest.'
    result = _strip_markdown_json_fence(text)
    assert result == '{"template": "use {name} here"}'


# ---------------------------------------------------------------------------
# complete_chat_once_with_json_retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_json_retry_returns_immediately_on_valid_json():
    """No retry should happen when the first response is already valid JSON."""
    from backend.services.llm_proxy import complete_chat_once_with_json_retry
    from unittest.mock import AsyncMock, patch

    mock = AsyncMock(return_value='{"ok": true}')
    with patch("backend.services.llm_proxy.complete_chat_once", mock):
        result = await complete_chat_once_with_json_retry(
            [{"role": "user", "content": "go"}],
            "test-model",
        )

    assert mock.call_count == 1
    assert '"ok"' in result


@pytest.mark.asyncio
async def test_json_retry_retries_on_non_json_first_response():
    """Should retry once when the first response is not valid JSON."""
    from backend.services.llm_proxy import complete_chat_once_with_json_retry
    from unittest.mock import AsyncMock, patch

    responses = [
        "Sure! I can help with that.",  # non-JSON
        '{"result": "ok"}',             # valid JSON
    ]
    mock = AsyncMock(side_effect=responses)
    with patch("backend.services.llm_proxy.complete_chat_once", mock):
        result = await complete_chat_once_with_json_retry(
            [{"role": "user", "content": "go"}],
            "test-model",
            max_retries=1,
        )

    assert mock.call_count == 2
    assert '"result"' in result


@pytest.mark.asyncio
async def test_json_retry_returns_last_response_after_all_retries_exhausted():
    """After all retries are exhausted the raw last response is returned."""
    from backend.services.llm_proxy import complete_chat_once_with_json_retry
    from unittest.mock import AsyncMock, patch

    mock = AsyncMock(return_value="Still not JSON.")
    with patch("backend.services.llm_proxy.complete_chat_once", mock):
        result = await complete_chat_once_with_json_retry(
            [{"role": "user", "content": "go"}],
            "test-model",
            max_retries=2,
        )

    # 1 initial call + 2 retries = 3 total
    assert mock.call_count == 3
    assert result == "Still not JSON."


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


# ---------------------------------------------------------------------------
# _is_within_sandbox — symlink escape guard
# ---------------------------------------------------------------------------

def test_is_within_sandbox_regular_file(tmp_path):
    from backend.routers.chat import _is_within_sandbox

    file = tmp_path / "scripts" / "run.py"
    file.parent.mkdir()
    file.write_text("pass")

    assert _is_within_sandbox(file, tmp_path.resolve()) is True


def test_is_within_sandbox_escape_rejected(tmp_path):
    from backend.routers.chat import _is_within_sandbox

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    scripts = tmp_path / "skill" / "scripts"
    scripts.mkdir(parents=True)
    link = scripts / "evil.py"
    link.symlink_to(outside)

    sandbox = (tmp_path / "skill").resolve()
    assert _is_within_sandbox(link, sandbox) is False


def test_is_within_sandbox_nested_path_ok(tmp_path):
    from backend.routers.chat import _is_within_sandbox

    nested = tmp_path / "skill" / "scripts" / "sub" / "run.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("pass")

    sandbox = (tmp_path / "skill").resolve()
    assert _is_within_sandbox(nested, sandbox) is True
