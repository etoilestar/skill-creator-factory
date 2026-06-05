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


def test_runtime_stdout_validation_is_role_aware():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    with pytest.raises(ValueError, match="非空 text"):
        _validate_stdout_against_action_entry(json.dumps({"file_paths": ["x.pdf"]}), {"role": "text_generator"})

    _validate_stdout_against_action_entry(json.dumps({"text": "done"}), {"role": "text_generator"})


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


def test_composite_generator_stdout_validates_required_capability_outputs():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    entry = {
        "role": "composite_generator",
        "required_capabilities": ["text_generation", "image_generation"],
        "outputs": ["text", "image_paths"],
    }

    with pytest.raises(ValueError, match="image_path"):
        _validate_stdout_against_action_entry(json.dumps({"text": "story"}), entry)

    _validate_stdout_against_action_entry(json.dumps({"text": "story", "image_paths": ["assets/generated/a.png"]}), entry)


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
