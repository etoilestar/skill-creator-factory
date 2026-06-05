import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _write_skill(root: Path, skill_md: str, *, reference: str | None = None, asset: tuple[str, bytes] | None = None) -> Path:
    skill_dir = root / "schema-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('{}')", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if reference is not None:
        (skill_dir / "references").mkdir(exist_ok=True)
        (skill_dir / "references" / "exec.md").write_text(reference, encoding="utf-8")
    if asset is not None:
        rel, data = asset
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return skill_dir


def test_runtime_action_schema_reads_reference_command_entry(tmp_path):
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_md = """---
name: schema-skill
description: demo
---
# 使用
读取 `references/exec.md` 中的执行步骤和命令模板。
"""
    reference = """# 执行参考
role: text_generator
inputs: topic
outputs: text
required_capabilities: text_generation
forbidden_capabilities: image_generation, pdf_generation

```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md, reference=reference)

    contract = _extract_skill_command_contract(skill_md, execution_root=skill_dir)

    assert contract["has_executable_command_block"]
    assert contract["command_blocks"][0]["source_path"] == "references/exec.md"
    assert contract["action_schema"]["errors"] == []
    assert contract["action_schema"]["entries"][0]["role"] == "text_generator"


def test_runtime_action_schema_rejects_conflicting_skill_and_reference_entries(tmp_path):
    from backend.routers.sandbox_chat import _build_runtime_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
role: text_generator
inputs: topic
outputs: text
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    reference = """# 执行参考
role: text_generator
inputs: prompt
outputs: text
```bash
python scripts/run.py '{"prompt":"{{prompt}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md, reference=reference)

    schema = _build_runtime_action_schema(skill_md, execution_root=skill_dir)

    assert any("多个不一致执行入口" in item["error"] for item in schema["errors"])


def test_runtime_command_validation_requires_declared_json_keys(tmp_path):
    from backend.routers.sandbox_chat import _validate_runtime_command_against_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
role: text_generator
inputs: topic
outputs: text
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md)

    with pytest.raises(ValueError, match="JSON keys"):
        _validate_runtime_command_against_action_schema(
            "python scripts/run.py '{\"topic\":\"cats\",\"extra\":\"no\"}'",
            execution_root=skill_dir,
        )

    entry = _validate_runtime_command_against_action_schema(
        "python scripts/run.py '{\"topic\":\"cats\"}'",
        execution_root=skill_dir,
    )
    assert entry["role"] == "text_generator"


def test_runtime_stdout_validation_is_field_name_agnostic():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    _validate_stdout_against_action_entry(json.dumps({"draft_copy": "done"}), {"role": "text_generator"})

    with pytest.raises(ValueError, match="至少需要一个非空字段"):
        _validate_stdout_against_action_entry(json.dumps({"draft_copy": ""}), {"role": "text_generator"})


def test_runtime_action_schema_validates_referenced_assets(tmp_path):
    from backend.routers.sandbox_chat import _build_runtime_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
# 使用
读取 `assets/config.json` 后执行。
role: generic_script
inputs: topic
outputs: text
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md, asset=("assets/config.json", b"not-json"))

    schema = _build_runtime_action_schema(skill_md, execution_root=skill_dir)

    assert any("assets/config.json" in item.get("asset_path", "") for item in schema["errors"])


def test_composite_generator_stdout_uses_field_name_agnostic_runtime_validation():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    entry = {
        "role": "composite_generator",
        "required_capabilities": ["text_generation", "image_generation"],
        "outputs": ["text", "image_paths"],
    }

    _validate_stdout_against_action_entry(json.dumps({"draft": "story"}), entry)

    with pytest.raises(ValueError, match="至少需要一个非空字段"):
        _validate_stdout_against_action_entry(json.dumps({"draft": ""}), entry)


def test_generic_script_high_impact_capability_error_mentions_explicit_roles():
    from backend.routers.sandbox_chat import _build_runtime_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
role: generic_script
inputs: topic
outputs: html_path
required_capabilities: html_generation
```bash
python scripts/build.py '{"topic":"{{topic}}"}'
```
"""
    schema = _build_runtime_action_schema(skill_md)

    assert any("html_asset_builder" in item.get("error", "") for item in schema["errors"])


def test_execute_plan_read_resource_then_final_instruction_run_command_uses_stdout(tmp_path):
    from backend.routers.sandbox_chat import (
        _execute_single_task,
        _extract_executable_command_blocks_from_text,
        _finalize_answer_output_file_links,
        _render_success_stdout_payload,
    )
    from backend.routers.chat_models import ChatRequest, Message

    skill_dir = tmp_path / "story-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "assets" / "generated").mkdir(parents=True)
    (skill_dir / "references" / "story_templates.md").write_text(
        "Use a short fable style.", encoding="utf-8"
    )
    (skill_dir / "scripts" / "generate.py").write_text(
        """
import json
import sys
from pathlib import Path

payload = json.loads(sys.argv[1])
out = Path('assets/generated/story.png')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(b'fake-png')
print(json.dumps({
    'text': f"Story about {payload['topic']}",
    'image_paths': [str(out)],
}))
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
name: story-skill
description: demo
---
Read `references/story_templates.md`, then run the declared command.
role: composite_generator
inputs: topic
outputs: text, image_paths
required_capabilities: text_generation, image_generation
forbidden_capabilities: pdf_generation
```bash
python scripts/generate.py '{"topic":"{{topic}}"}'
```
""",
        encoding="utf-8",
    )

    request = ChatRequest(messages=[Message(role="user", content="写一个狐狸故事")])
    read_result, _ = _execute_single_task(
        {"action": "read_resource", "path": "references/story_templates.md", "reason": "style"},
        [],
        request,
        execution_root=skill_dir,
        skill_name="story-skill",
    )
    assert read_result["success"] is True
    assert "short fable" in read_result["content"]

    final_instruction = """Use the loaded reference and execute:
```bash
python scripts/generate.py '{"topic":"狐狸"}'
```
"""
    commands = _extract_executable_command_blocks_from_text(final_instruction)
    assert commands == ["python scripts/generate.py '{\"topic\":\"狐狸\"}'"]

    run_result, _ = _execute_single_task(
        {"action": "run_command", "command": commands[0], "reason": "final_instruction"},
        [],
        request,
        execution_root=skill_dir,
        skill_name="story-skill",
    )
    assert run_result["success"] is True
    assert run_result["action"] == "run_command"
    assert json.loads(run_result["stdout"])["image_paths"] == ["assets/generated/story.png"]
    assert run_result["output_files"] == [
        {
            "path": "assets/generated/story.png",
            "url": "/api/skills/story-skill/files/assets/generated/story.png",
        }
    ]

    exec_result = {"results": [read_result, run_result], "output_files": run_result["output_files"]}
    answer = _render_success_stdout_payload(exec_result)
    answer = _finalize_answer_output_file_links(answer, exec_result["output_files"])

    assert "Story about 狐狸" in answer
    assert "/api/skills/story-skill/files/assets/generated/story.png" in answer


def test_final_instruction_accepts_plain_single_line_command():
    from backend.routers.sandbox_chat import _extract_executable_command_blocks_from_text

    final_instruction = "请执行下面命令：\npython scripts/generate.py '{\"topic\":\"狐狸\"}'\n完成后使用 stdout JSON。"

    assert _extract_executable_command_blocks_from_text(final_instruction) == [
        "python scripts/generate.py '{\"topic\":\"狐狸\"}'"
    ]


def test_runtime_optional_inputs_may_be_omitted_or_empty(tmp_path):
    from backend.routers.sandbox_chat import _validate_runtime_command_against_action_schema

    skill_md = """---
name: optional-skill
description: demo
---
role: composite_generator
inputs: topic, style, custom_character?
optional_inputs: custom_character
outputs: text, image_paths
required_capabilities: text_generation, image_generation
```bash
python scripts/run.py '{"topic":"{{topic}}","style":"{{style}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md)

    entry = _validate_runtime_command_against_action_schema(
        "python scripts/run.py '{\"topic\":\"cats\",\"style\":\"ink\"}'",
        execution_root=skill_dir,
    )
    assert entry["optional_inputs"] == ["custom_character"]

    _validate_runtime_command_against_action_schema(
        "python scripts/run.py '{\"topic\":\"cats\",\"style\":\"ink\",\"custom_character\":\"\"}'",
        execution_root=skill_dir,
    )

    with pytest.raises(ValueError, match="JSON keys"):
        _validate_runtime_command_against_action_schema(
            "python scripts/run.py '{\"topic\":\"cats\",\"style\":\"ink\",\"extra\":\"no\"}'",
            execution_root=skill_dir,
        )


def test_planner_missing_available_script_is_corrected(tmp_path):
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    skill_dir = tmp_path / "available-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('{}')", encoding="utf-8")

    plan = _normalize_skill_runtime_plan(
        {
            "mode": "ask_user",
            "actions": [],
            "missing": [{"path": "scripts/run.py", "reason": "planner says missing"}],
            "errors": [],
        },
        resource_catalog=[],
        execution_root=skill_dir,
        command_contract={"has_executable_command_block": True, "action_schema": {"entries": []}},
        available_scripts=["scripts/run.py"],
    )

    assert plan["missing"] == []
    assert plan["mode"] == "direct_answer"
    assert plan["planner_inconsistent"][0]["missing_type"] == "planner_inconsistent"


def test_structured_stdout_accepts_story_text_alias():
    from backend.routers.sandbox_chat import _render_success_stdout_payload, _validate_stdout_against_action_entry

    entry = {
        "role": "composite_generator",
        "required_capabilities": ["text_generation", "image_generation"],
        "outputs": ["story_text", "image_paths"],
    }
    stdout = json.dumps({"story_text": "A real story", "image_paths": ["outputs/a.png"]})

    _validate_stdout_against_action_entry(stdout, entry)
    assert "A real story" in _render_success_stdout_payload({"results": [{"success": True, "stdout": stdout}]})


def test_runtime_stdout_validation_accepts_multifunction_export_fields():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    _validate_stdout_against_action_entry(
        json.dumps({"story_text": "once", "image_paths": ["outputs/img.png"]}),
        {"role": "composite_generator", "outputs": ["story_text", "image_paths"]},
    )
    _validate_stdout_against_action_entry(
        json.dumps({"docx_path": "assets/generated/story.docx"}),
        {"role": "docx_builder", "outputs": ["docx_path"]},
    )
    _validate_stdout_against_action_entry(
        json.dumps({"pptx_path": "assets/generated/story.pptx"}),
        {"role": "pptx_builder", "outputs": ["pptx_path"]},
    )


def test_output_files_from_stdout_json_collects_all_artifact_fields(tmp_path):
    from backend.routers.sandbox_chat import _output_files_from_stdout_json

    skill_dir = tmp_path / "schema-skill"
    generated = skill_dir / "assets" / "generated"
    outputs = skill_dir / "outputs"
    generated.mkdir(parents=True)
    outputs.mkdir(parents=True)
    from zipfile import ZipFile, ZIP_DEFLATED
    (skill_dir / "assets/generated/story.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    with ZipFile(skill_dir / "assets/generated/story.docx", "w", ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    with ZipFile(skill_dir / "assets/generated/story.pptx", "w", ZIP_DEFLATED) as zf:
        zf.writestr("ppt/presentation.xml", "<p:presentation/>")
    (skill_dir / "assets/generated/story.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    (skill_dir / "outputs/img.png").write_bytes(b"data")

    files = _output_files_from_stdout_json(
        json.dumps({
            "pdf_path": "assets/generated/story.pdf",
            "docx_path": "assets/generated/story.docx",
            "pptx_path": "assets/generated/story.pptx",
            "html_path": "assets/generated/story.html",
            "image_paths": ["outputs/img.png"],
        }),
        cwd=skill_dir,
        skill_name="schema-skill",
    )

    assert {item["path"] for item in files} == {
        "assets/generated/story.pdf",
        "assets/generated/story.docx",
        "assets/generated/story.pptx",
        "assets/generated/story.html",
        "outputs/img.png",
    }

def test_file_output_validation_accepts_arbitrary_field_names(tmp_path):
    from backend.routers.sandbox_chat import _output_files_from_stdout_json
    from backend.services.artifact_validator import validate_stdout_file_outputs

    skill_dir = tmp_path / "schema-skill"
    generated = skill_dir / "assets" / "generated"
    generated.mkdir(parents=True)
    report = generated / "custom-report.pdf"
    report.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")

    stdout = json.dumps({"business_report": "assets/generated/custom-report.pdf"})

    assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [
        {"path": "assets/generated/custom-report.pdf"}
    ]
    assert _output_files_from_stdout_json(stdout, cwd=skill_dir, skill_name="schema-skill") == [
        {"path": "assets/generated/custom-report.pdf", "url": "/api/skills/schema-skill/files/assets/generated/custom-report.pdf"}
    ]


def test_skill_dataflow_validates_stdout_fields_against_skill_placeholders():
    from backend.services.skill_dataflow import extract_skill_commands, validate_dataflow_closed

    skill_md = """---
name: flow-skill
description: demo
---
```bash
python scripts/write.py '{"topic":"{{topic}}"}'
```
```bash
python scripts/render.py '{"draft":"{{draft}}"}'
```
"""

    commands = extract_skill_commands(skill_md)
    final_context = validate_dataflow_closed(
        commands,
        initial_context={"topic": "cats"},
        stdout_by_script={"scripts/write.py": json.dumps({"draft": "a cat tale"})},
    )

    assert final_context["draft"] == "a cat tale"


def _write_workflow_skill(root: Path) -> tuple[Path, dict]:
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_dir = root / "workflow-skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "generate_story.py").write_text(
        """
import json, sys
payload = json.loads(sys.argv[1])
print(json.dumps({"story_text": "story about " + payload["topic"], "chapters": [{"title":"A", "content":"one"}, {"title":"B", "content":"two"}]}))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "generate_illustration.py").write_text(
        """
import hashlib, json, sys
payload = json.loads(sys.argv[1])
name = hashlib.sha1(payload["chapter_text"].encode()).hexdigest()[:8] + ".png"
path = "outputs/" + name
import pathlib
(pathlib.Path("outputs")).mkdir(exist_ok=True)
pathlib.Path(path).write_bytes(b"png")
print(json.dumps({"image_path": path}))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "build_pdf.py").write_text(
        """
import json, pathlib, sys
payload = json.loads(sys.argv[1])
path = pathlib.Path("outputs/story.pdf")
path.parent.mkdir(exist_ok=True)
path.write_bytes(b"%PDF-1.4\\n%%EOF")
print(json.dumps({"pdf_path": "outputs/story.pdf", "used_images": payload.get("image_paths", [])}))
""".strip(),
        encoding="utf-8",
    )
    skill_md = """---
name: workflow-skill
description: demo
---
步骤1 生成故事。
role: text_generator
inputs: topic
outputs: story_text, chapters
```bash
python scripts/generate_story.py '{"topic":"{{topic}}"}'
```

步骤2 遍历 chapters 中的每一章 chapter 生成插图。
role: image_generator
inputs: chapter_text
outputs: image_path
required_capabilities: image_generation
```bash
python scripts/generate_illustration.py '{"chapter_text":"{{chapter_text}}"}'
```

步骤3 构建 PDF。
role: pdf_builder
inputs: story_text, image_paths
outputs: pdf_path
required_capabilities: pdf_generation
```bash
python scripts/build_pdf.py '{"story_text":"{{story_text}}","image_paths":"{{image_paths}}"}'
```
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return skill_dir, _extract_skill_command_contract(skill_md, execution_root=skill_dir)


def test_multi_script_skill_forces_execute_workflow(tmp_path):
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    skill_dir, contract = _write_workflow_skill(tmp_path)

    result = _normalize_skill_runtime_plan(
        {"mode": "direct_answer", "actions": [], "errors": [], "missing": []},
        execution_root=skill_dir,
        available_scripts=["scripts/generate_story.py", "scripts/generate_illustration.py", "scripts/build_pdf.py"],
        command_contract=contract,
        user_text="生成一个带插图的 PDF 故事",
    )

    assert result["mode"] == "execute_workflow"
    assert result["workflow_actions"][0]["action"] == "run_command"
    assert result["workflow_actions"][0]["script_path"] == "scripts/generate_story.py"
    assert result["final_instruction"] == ""


def test_render_command_template_uses_previous_stdout_context():
    from backend.routers.sandbox_chat import merge_step_output, render_command_template

    context = {"topic": "狐狸"}
    merge_step_output(context, "scripts/generate_story.py", {"story_text": "狐狸故事", "chapters": []})

    rendered = render_command_template(
        "python scripts/build_pdf.py '{\"story_text\":\"{{story_text}}\"}'",
        context,
    )

    import shlex
    assert json.loads(shlex.split(rendered)[2])["story_text"] == "狐狸故事"


def test_execute_skill_workflow_foreach_collects_images_and_pdf(tmp_path):
    import asyncio
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_workflow_skill(tmp_path)
    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"topic": "狐狸", "user_request": "生成 PDF"},
        request=ChatRequest(messages=[Message(role="user", content="生成 PDF")]),
        skill_name="workflow-skill",
    ))

    assert result["executed"] is True
    assert len([r for r in result["results"] if r["action"] == "run_command"]) == 4
    assert len(result["context"]["image_paths"]) == 2
    assert any(item["path"] == "outputs/story.pdf" for item in result["output_files"])
    assert (skill_dir / "outputs" / "story.pdf").is_file()


def test_render_command_template_reports_dataflow_mismatch():
    from backend.routers.sandbox_chat import render_command_template

    with pytest.raises(ValueError, match="dataflow_mismatch"):
        render_command_template(
            "python scripts/build_pdf.py '{\"image_paths\":\"{{image_paths}}\"}'",
            {},
        )


def _write_initial_context_workflow_skill(root: Path) -> tuple[Path, dict]:
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_dir = root / "initial-context-skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "generate_story.py").write_text(
        """
import json, sys
payload = json.loads(sys.argv[1])
chapters = [{"title": str(i + 1), "content": payload["story_theme"] + str(i + 1)} for i in range(int(payload["chapter_count"]))]
print(json.dumps({"story_text": payload["story_theme"], "chapters": chapters, "audience": payload["target_audience"]}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "build_pdf.py").write_text(
        """
import json, pathlib, sys
payload = json.loads(sys.argv[1])
path = pathlib.Path("outputs/initial.pdf")
path.parent.mkdir(exist_ok=True)
path.write_bytes(b"%PDF-1.4\\n%%EOF")
print(json.dumps({"pdf_path": "outputs/initial.pdf", "chapter_total": len(payload["chapters"])}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    skill_md = """---
name: initial-context-skill
description: demo
---
role: text_generator
inputs: story_theme, chapter_count, target_audience
outputs: story_text, chapters
```bash
python scripts/generate_story.py '{"story_theme":"{{story_theme}}","chapter_count":"{{chapter_count}}","target_audience":"{{target_audience}}"}'
```

role: pdf_builder
inputs: story_text, chapters
outputs: pdf_path
required_capabilities: pdf_generation
```bash
python scripts/build_pdf.py '{"story_text":"{{story_text}}","chapters":"{{chapters}}"}'
```
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return skill_dir, _extract_skill_command_contract(skill_md, execution_root=skill_dir)


def test_execute_skill_workflow_infers_first_step_context_from_user_input(tmp_path):
    import asyncio
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_initial_context_workflow_skill(tmp_path)
    request_text = "请以海底冒险为主题，写3章，面向小学生，并生成PDF。"

    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"user_request": request_text},
        request=ChatRequest(messages=[Message(role="user", content=request_text)]),
        skill_name="initial-context-skill",
    ))

    assert result["results"][0]["action"] == "run_command"
    assert "scripts/generate_story.py" in result["results"][0]["command"]
    assert result["context"]["story_theme"] == "海底冒险"
    assert result["context"]["chapter_count"] == 3
    assert result["context"]["target_audience"] == "小学生"
    assert len(result["context"]["chapters"]) == 3
    assert any(item["path"] == "outputs/initial.pdf" for item in result["output_files"])


def test_execute_skill_workflow_reports_initial_input_parse_failure(tmp_path):
    import asyncio
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_initial_context_workflow_skill(tmp_path)
    request_text = "请生成图文PDF。"

    with pytest.raises(ValueError, match="初始输入解析失败"):
        asyncio.run(_execute_skill_workflow(
            execution_root=skill_dir,
            action_schema=contract["action_schema"],
            user_context={"user_request": request_text},
            request=ChatRequest(messages=[Message(role="user", content=request_text)]),
            skill_name="initial-context-skill",
        ))


def _write_full_dataflow_workflow_skill(root: Path) -> tuple[Path, dict]:
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_dir = root / "full-dataflow-skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "story_step.py").write_text(
        """
import json, sys
payload = json.loads(sys.argv[1])
chapters = [
    {"title": f"Chapter {i + 1}", "content": f"{payload['story_theme']} #{i + 1} for {payload['target_audience']}"}
    for i in range(int(payload["chapter_count"]))
]
print(json.dumps({"story_text": payload["story_theme"], "chapter_list": chapters}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "image_step.py").write_text(
        """
import hashlib, json, pathlib, sys
payload = json.loads(sys.argv[1])
name = hashlib.sha1(payload["chapter_text"].encode()).hexdigest()[:8] + ".png"
path = pathlib.Path("outputs") / name
path.parent.mkdir(exist_ok=True)
path.write_bytes(b"png")
print(json.dumps({"image_path": path.as_posix()}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "pdf_step.py").write_text(
        """
import json, pathlib, sys
payload = json.loads(sys.argv[1])
path = pathlib.Path("outputs/full-dataflow.pdf")
path.parent.mkdir(exist_ok=True)
path.write_bytes(b"%PDF-1.4\\n%%EOF")
print(json.dumps({"pdf_path": path.as_posix(), "image_count": len(payload["image_paths"])}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    skill_md = """---
name: full-dataflow-skill
description: demo
---
role: text_generator
inputs: story_theme, chapter_count=2, target_audience=小学生
outputs: story_text, chapter_list
```bash
python scripts/story_step.py '{"story_theme":"{{story_theme}}","chapter_count":"{{chapter_count}}","target_audience":"{{target_audience}}"}'
```

role: image_generator
inputs: chapter_text
outputs: image_path
required_capabilities: image_generation
```bash
python scripts/image_step.py '{"chapter_text":"{{chapter_text}}"}'
```

role: pdf_builder
inputs: story_text, image_paths
outputs: pdf_path
required_capabilities: pdf_generation
```bash
python scripts/pdf_step.py '{"story_text":"{{story_text}}","image_paths":"{{image_paths}}"}'
```
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return skill_dir, _extract_skill_command_contract(skill_md, execution_root=skill_dir)


def test_execute_skill_workflow_full_variable_chain_from_input_defaults_to_final_file(tmp_path):
    import asyncio
    import shlex
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow, _normalize_skill_runtime_plan

    skill_dir, contract = _write_full_dataflow_workflow_skill(tmp_path)
    runtime_plan = _normalize_skill_runtime_plan(
        {"mode": "direct_answer", "actions": [], "errors": [], "missing": [], "final_instruction": "ignored"},
        execution_root=skill_dir,
        available_scripts=["scripts/story_step.py", "scripts/image_step.py", "scripts/pdf_step.py"],
        command_contract=contract,
        user_text="请以月球种菜为主题生成图文 PDF。",
    )
    assert runtime_plan["mode"] == "execute_workflow"
    assert [item["script_path"] for item in runtime_plan["workflow_actions"]] == [
        "scripts/story_step.py",
        "scripts/image_step.py",
        "scripts/pdf_step.py",
    ]

    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"user_request": "请以月球种菜为主题生成图文 PDF。"},
        request=ChatRequest(messages=[Message(role="user", content="请以月球种菜为主题生成图文 PDF。")]),
        skill_name="full-dataflow-skill",
    ))

    first_payload = json.loads(shlex.split(result["results"][0]["command"])[2])
    assert first_payload == {"story_theme": "月球种菜", "chapter_count": 2, "target_audience": "小学生"}
    assert len([r for r in result["results"] if r["action"] == "run_command"]) == 4
    assert len(result["context"]["chapter_list"]) == 2
    assert len(result["context"]["image_paths"]) == 2
    third_payload = json.loads(shlex.split(result["results"][-1]["command"])[2])
    assert third_payload["story_text"] == "月球种菜"
    assert third_payload["image_paths"] == result["context"]["image_paths"]
    assert any(item["path"] == "outputs/full-dataflow.pdf" for item in result["output_files"])
    assert (skill_dir / "outputs" / "full-dataflow.pdf").is_file()
