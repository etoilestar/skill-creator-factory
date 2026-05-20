"""Tests for pure helper functions in backend/routers/creator.py."""

import pytest


# ---------------------------------------------------------------------------
# _parse_tool_call
# ---------------------------------------------------------------------------

def test_parse_tool_call_valid_run_script():
    from backend.routers.creator import _parse_tool_call

    text = '{"tool_call": {"action": "run_script", "name": "my-skill", "filename": "main.py", "args": []}}'
    result = _parse_tool_call(text)
    assert result is not None
    assert result["action"] == "run_script"
    assert result["name"] == "my-skill"
    assert result["filename"] == "main.py"


def test_parse_tool_call_valid_validate():
    from backend.routers.creator import _parse_tool_call

    text = '{"tool_call": {"action": "validate", "name": "my-skill"}}'
    result = _parse_tool_call(text)
    assert result is not None
    assert result["action"] == "validate"


def test_parse_tool_call_valid_init():
    from backend.routers.creator import _parse_tool_call

    text = '{"tool_call": {"action": "init", "name": "my-skill"}}'
    result = _parse_tool_call(text)
    assert result is not None
    assert result["action"] == "init"


def test_parse_tool_call_valid_package():
    from backend.routers.creator import _parse_tool_call

    text = '{"tool_call": {"action": "package", "name": "my-skill"}}'
    result = _parse_tool_call(text)
    assert result is not None
    assert result["action"] == "package"


def test_parse_tool_call_disallowed_action():
    """write and write_file are intentionally blocked — user must preview first."""
    from backend.routers.creator import _parse_tool_call

    for action in ("write", "write_file", "delete", "exec"):
        text = f'{{"tool_call": {{"action": "{action}", "name": "x"}}}}'
        assert _parse_tool_call(text) is None, f"action={action!r} should be rejected"


def test_parse_tool_call_plain_file_content():
    """Normal Python code must not be parsed as a tool call."""
    from backend.routers.creator import _parse_tool_call

    code = "import sys\n\ndef main():\n    print('hello')\n\nmain()"
    assert _parse_tool_call(code) is None


def test_parse_tool_call_markdown_content():
    from backend.routers.creator import _parse_tool_call

    md = "# Title\n\nSome content here.\n\n- item 1\n- item 2\n"
    assert _parse_tool_call(md) is None


def test_parse_tool_call_invalid_json():
    from backend.routers.creator import _parse_tool_call

    assert _parse_tool_call("{not valid json}") is None


def test_parse_tool_call_empty_string():
    from backend.routers.creator import _parse_tool_call

    assert _parse_tool_call("") is None


def test_parse_tool_call_no_tool_call_key():
    """Valid JSON but missing the top-level 'tool_call' key."""
    from backend.routers.creator import _parse_tool_call

    text = '{"action": "run_script", "name": "my-skill"}'
    assert _parse_tool_call(text) is None


def test_parse_tool_call_whitespace_padded():
    """Leading/trailing whitespace should be stripped before parsing."""
    from backend.routers.creator import _parse_tool_call

    text = '  {"tool_call": {"action": "run_script", "name": "s", "filename": "a.py", "args": []}}  '
    result = _parse_tool_call(text)
    assert result is not None
    assert result["action"] == "run_script"


# ---------------------------------------------------------------------------
# _build_generate_file_prompt — tool-call addendum present in every variant
# ---------------------------------------------------------------------------

def test_build_prompt_skill_md_contains_tool_call_note():
    from backend.routers.creator import _build_generate_file_prompt

    msgs = _build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="demo",
        purpose="main doc",
        blueprint_text="blueprint here",
        conversation_history=[],
    )
    system_content = msgs[0]["content"]
    assert "tool_call" in system_content
    assert "run_script" in system_content


def test_build_prompt_scripts_contains_tool_call_note():
    from backend.routers.creator import _build_generate_file_prompt

    msgs = _build_generate_file_prompt(
        file_path="scripts/main.py",
        skill_name="demo",
        purpose="main script",
        blueprint_text="blueprint here",
        conversation_history=[],
    )
    system_content = msgs[0]["content"]
    assert "tool_call" in system_content


def test_build_prompt_references_contains_tool_call_note():
    from backend.routers.creator import _build_generate_file_prompt

    msgs = _build_generate_file_prompt(
        file_path="references/guide.md",
        skill_name="demo",
        purpose="reference doc",
        blueprint_text="blueprint here",
        conversation_history=[],
    )
    system_content = msgs[0]["content"]
    assert "tool_call" in system_content


def test_build_prompt_assets_contains_tool_call_note():
    from backend.routers.creator import _build_generate_file_prompt

    msgs = _build_generate_file_prompt(
        file_path="assets/config.json",
        skill_name="demo",
        purpose="config asset",
        blueprint_text="blueprint here",
        conversation_history=[],
    )
    system_content = msgs[0]["content"]
    assert "tool_call" in system_content
