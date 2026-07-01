from dataclasses import dataclass, field

from backend.services.creator.command_normalizer import (
    canonicalize_skill_md_runtime_commands,
    parse_skill_md_bash_command_blocks,
    replace_skill_md_command_block,
    validate_runtime_command_format,
)
from backend.services.creator.e2e import _is_skill_md_command_format_error


@dataclass
class Entry:
    path: str
    command_template: str = ""
    runtime_contract: dict = field(default_factory=dict)


@dataclass
class RuntimeSpec:
    command_template: str = ""
    script_argv_schema: dict = field(default_factory=dict)


def test_valid_command_block_unchanged():
    skill_md = """# Skill
```bash
python scripts/run.py '{"value":"{{value}}"}'
```
"""
    result = canonicalize_skill_md_runtime_commands(skill_name="s", skill_md=skill_md)
    assert not result.changed
    assert not result.blocked
    assert result.content == skill_md


def test_invalid_json_argv_replaced_from_verified_command_template():
    skill_md = """Before
```bash
python scripts/run.py '{"value": {{value | default("x")}}}'
```
After
"""
    result = canonicalize_skill_md_runtime_commands(
        skill_name="s",
        skill_md=skill_md,
        runtime_specs={"scripts/run.py": RuntimeSpec(command_template="python scripts/run.py '{\"value\":\"{{value}}\"}'")},
    )
    assert result.changed
    assert not result.blocked
    assert "{{value | default" not in result.content
    assert "python scripts/run.py '{\"value\":\"{{value}}\"}'" in result.content


def test_complex_template_expression_is_not_written_back_to_json_argv():
    skill_md = """```bash
python scripts/run.py '{"value":"{{ value | default(\"fallback\") }}"}'
```
"""
    result = canonicalize_skill_md_runtime_commands(
        skill_name="s",
        skill_md=skill_md,
        files=[Entry(path="scripts/run.py", command_template="python scripts/run.py '{\"value\":\"{{value}}\"}'")],
    )
    assert result.changed
    assert "default(" not in result.content
    assert validate_runtime_command_format("python scripts/run.py '{\"value\":\"{{value}}\"}'") == []


def test_missing_binding_returns_missing_command_arg_binding():
    skill_md = """```bash
python scripts/run.py '{"value": {{value | default("x")}}}'
```
"""
    result = canonicalize_skill_md_runtime_commands(
        skill_name="s",
        skill_md=skill_md,
        runtime_specs={"scripts/run.py": RuntimeSpec(script_argv_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]})},
    )
    assert result.blocked
    assert any(issue.code == "missing_command_arg_binding" for issue in result.issues)


def test_does_not_infer_args_from_script_name_or_business_words():
    skill_md = """# Business words mention value and topic.
```bash
python scripts/topic_value.py '{"value": {{value | default("x")}}}'
```
"""
    result = canonicalize_skill_md_runtime_commands(skill_name="s", skill_md=skill_md)
    assert result.blocked
    assert any(issue.code == "missing_command_arg_binding" for issue in result.issues)
    assert "topic_value" in result.content
    assert "{{topic}}" not in result.content


def test_only_replaces_target_command_block():
    skill_md = """Intro
```bash
python scripts/first.py '{"x": {{x | default("x")}}}'
```
Middle
```bash
python scripts/second.py '{"y":"{{y}}"}'
```
"""
    result = canonicalize_skill_md_runtime_commands(
        skill_name="s",
        skill_md=skill_md,
        runtime_specs={"scripts/first.py": RuntimeSpec(command_template="python scripts/first.py '{\"x\":\"{{x}}\"}'")},
    )
    assert result.changed
    assert "python scripts/first.py '{\"x\":\"{{x}}\"}'" in result.content
    assert "python scripts/second.py '{\"y\":\"{{y}}\"}'" in result.content
    assert result.content.count("```bash") == 2


def test_replace_single_block_preserves_other_markdown():
    skill_md = "A\n```bash\npython scripts/a.py '{bad'\n```\nB\n"
    block = parse_skill_md_bash_command_blocks(skill_md)[0]
    updated = replace_skill_md_command_block(skill_md, block, "python scripts/a.py '{}'")
    assert updated == "A\n```bash\npython scripts/a.py '{}'\n```\nB\n"


def test_normalizer_blocked_command_format_error_does_not_enter_llm_patch_route():
    errors = [
        "E2E_REPAIR_TARGET=SKILL.md\nE2E_LAYER=command_json_parse\nE2E_STRUCTURED_FAILURE={}\ninvalid_json_arg"
    ]
    assert _is_skill_md_command_format_error(errors)


def test_requirement_graph_from_to_field_edges_render_required_schema_command():
    graph = type("Graph", (), {"dataflow_edges": [
        {"from_node": "upstream", "from_field": "source_value", "to_node": "runner", "to_field": "value"}
    ]})()
    skill_md = """```bash
python scripts/run.py '{"value": {{value | default("x")}}}'
```
"""
    result = canonicalize_skill_md_runtime_commands(
        skill_name="s",
        skill_md=skill_md,
        runtime_specs={"scripts/run.py": RuntimeSpec(script_argv_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]})},
        requirement_graph=graph,
    )
    assert result.changed
    assert not result.blocked
    assert "{{upstream.source_value}}" in result.content


def test_e2e_normalizer_helper_passes_contract_context_for_template_replacement(tmp_path, monkeypatch):
    from backend.services.creator import e2e

    skill_dir = tmp_path / "skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('{}')\n", encoding="utf-8")
    skill_md = """```bash
python scripts/run.py '{"value": {{value | default("x")}}}'
```
"""

    monkeypatch.setattr(
        e2e,
        "_skill_plan_entry_for_file",
        lambda *, file_path, blueprint_text: Entry(
            path=file_path,
            command_template="python scripts/run.py '{\"value\":\"{{value}}\"}'",
        ),
    )

    result = e2e._normalize_skill_md_runtime_commands_for_e2e(
        skill_name="s",
        skill_dir=skill_dir,
        skill_md=skill_md,
    )
    assert result.changed
    assert not result.blocked
    assert "python scripts/run.py '{\"value\":\"{{value}}\"}'" in result.content


def test_from_to_field_edges_still_block_when_required_binding_missing():
    graph = type("Graph", (), {"dataflow_edges": [
        {"from_node": "upstream", "from_field": "source_value", "to_node": "runner", "to_field": "other"}
    ]})()
    skill_md = """```bash
python scripts/run.py '{"value": {{value | default("x")}}}'
```
"""
    result = canonicalize_skill_md_runtime_commands(
        skill_name="s",
        skill_md=skill_md,
        runtime_specs={"scripts/run.py": RuntimeSpec(script_argv_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]})},
        requirement_graph=graph,
    )
    assert result.blocked
    assert any(issue.code == "missing_command_arg_binding" for issue in result.issues)


def test_json_argv_input_files_template_is_safely_sanitized():
    skill_md = """```bash
python scripts/extract.py '{"input_file":"{{input_files[0]}}","model":"{{model}}"}'
```
"""
    result = canonicalize_skill_md_runtime_commands(skill_name="s", skill_md=skill_md)
    assert result.changed
    assert not result.blocked
    assert "{{input_files[0]}}" not in result.content
    assert "__RUNTIME_INPUT_FILE__" in result.content
    assert "TEXT_MODEL" in result.content
    assert "argv JSON contract" in result.content


def test_json_argv_reference_template_is_safely_sanitized_to_path():
    skill_md = """```bash
python scripts/extract.py '{"parse_rules":"{{references/parse_rules.md}}"}'
```
"""
    result = canonicalize_skill_md_runtime_commands(skill_name="s", skill_md=skill_md)
    assert result.changed
    assert not result.blocked
    assert "{{references/parse_rules.md}}" not in result.content
    assert '"parse_rules":"references/parse_rules.md"' in result.content


def test_standard_safe_argv_command_passes_without_contract_source():
    command = "python scripts/extract.py '{\"input_file\":\"__RUNTIME_INPUT_FILE__\",\"parse_rules\":\"references/parse_rules.md\",\"model\":\"TEXT_MODEL\"}'"
    assert validate_runtime_command_format(command) == []


def test_sanitized_contract_is_written_outside_bash_fence_and_validates_idempotently():
    skill_md = """Before
```bash
python scripts/extract.py '{"input_file":"{{input_files[0]}}","parse_rules":"{{references/parse_rules.md}}","model":"{{model}}"}'
```
After
"""
    first = canonicalize_skill_md_runtime_commands(skill_name="s", skill_md=skill_md)
    assert first.changed
    assert not first.blocked

    blocks = parse_skill_md_bash_command_blocks(first.content)
    assert len(blocks) == 1
    assert "argv JSON contract" not in blocks[0].content
    assert "**argv JSON contract**" in first.content
    assert validate_runtime_command_format(blocks[0].content) == []

    second = canonicalize_skill_md_runtime_commands(skill_name="s", skill_md=first.content)
    assert not second.changed
    assert not second.blocked
    assert second.content == first.content
