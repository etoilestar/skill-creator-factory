import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.routers.creator import _build_script_generate_file_prompt_variant
from backend.services.creator_contracts import compile_canonical_file_contract, resolve_implementation
from backend.services.runtime_tools.document_tools import create_text_file
from backend.services.skill_plan import SkillPlanEntry


def _prompt_text(entry: dict) -> str:
    messages = _build_script_generate_file_prompt_variant(
        file_path=entry["path"],
        skill_name="demo",
        purpose=entry.get("purpose", "demo"),
        blueprint_text="",
        role=entry.get("role"),
        skill_plan_entry=entry,
        variant="standard",
    )
    return "\n".join(str(m.get("content") or "") for m in messages)


def test_selected_tool_prompt_contains_schema_and_snippet():
    text = _prompt_text({
        "path": "scripts/make_text.py",
        "purpose": "generate text",
        "file_type": "script",
        "file_kind": "script",
        "role": "text_generator",
        "runtime": "python",
        "language": "python",
        "inputs": ["prompt"],
        "outputs": ["text"],
        "required_capabilities": ["text_generation"],
    })

    assert "tool_function_cards" in text
    assert "Input schema:" in text
    assert "Output schema:" in text
    assert "Tool Snippet" in text
    assert "helper returns str" in text.lower()
    assert "Do not use result.get('text')" in text
    assert '"type": "string"' in text
    assert "Raw helper return value" in text


def test_structural_fallback_preserves_tool_io_and_snippets(monkeypatch):
    monkeypatch.setattr("backend.services.creator_contracts._embedding_candidate_tool_ids", lambda requirements: set())
    entry = SkillPlanEntry(
        path="scripts/write_txt.py",
        purpose="write a txt file",
        file_type="script",
        file_kind="script",
        role="generic_script",
        runtime="python",
        language="python",
        inputs=["text"],
        outputs=["text_path", "file_outputs"],
        required_capabilities=["file_output"],
        side_effects=["write_output_file"],
        artifact_contract={"file_outputs": True},
    )
    stdout_schema = {"type": "object", "required": ["text_path", "file_outputs"], "properties": {"text_path": {"type": "string"}, "file_outputs": {"type": "array"}}}
    contract = compile_canonical_file_contract(entry, stdout_schema)
    resolution = resolve_implementation(entry, contract)

    assert resolution.available_tools
    tool = resolution.available_tools[0]
    assert tool.input_schema
    assert tool.output_schema
    assert tool.return_contract
    assert tool.snippets
    assert tool.common_mistakes


def test_file_output_tool_requires_file_outputs_and_helper_writes_txt(tmp_path):
    text = _prompt_text({
        "path": "scripts/write_txt.py",
        "purpose": "write text file",
        "file_type": "script",
        "file_kind": "script",
        "role": "generic_script",
        "runtime": "python",
        "language": "python",
        "inputs": ["text"],
        "outputs": ["text_path", "file_outputs"],
        "required_capabilities": ["file_output"],
        "side_effects": ["write_output_file"],
        "artifact_contract": {"file_outputs": True},
    })

    assert "create_text_file" in text
    assert "file_outputs" in text
    assert "Path(...).write_text" in text

    result = create_text_file("hello", filename="demo", output_dir=tmp_path)
    assert result == {
        "text_path": str(tmp_path / "demo.txt"),
        "file_paths": [str(tmp_path / "demo.txt")],
        "file_outputs": [str(tmp_path / "demo.txt")],
    }
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "hello"
