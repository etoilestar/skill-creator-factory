import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest


def test_skill_plan_parses_explicit_contract_fields_and_reference_mapping():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: poster-skill
- scripts/: `scripts/render.py`
  scripts/render.py
  role: image_generator
  inputs: topic, prompt
  outputs: image_paths, images
  dependencies: references/image-generation.md
  required_capabilities: image_generation
  forbidden_capabilities: pdf_generation
- references/: `references/image-generation.md`
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/render.py")

    assert entry.role == "image_generator"
    assert entry.inputs == ["topic", "prompt"]
    assert entry.outputs == ["image_paths", "images"]
    assert entry.dependencies == ["references/image-generation.md"]
    assert entry.required_capabilities == ["image_generation"]
    assert entry.forbidden_capabilities == ["pdf_generation"]


def test_low_confidence_script_falls_back_to_generic_with_warning():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: vague-skill
- scripts/: `scripts/main.py` 生成图片和 PDF，但未声明 role
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/main.py")

    assert entry.role == "generic_script"
    assert entry.confidence < 0.7
    assert any("generic_script" in warning for warning in plan.warnings)
    assert "image_generation" not in entry.required_capabilities


def test_skill_md_command_block_must_match_skillplan_inputs():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: text-skill
- scripts/: `scripts/write.py`
  scripts/write.py role: text_generator inputs: topic, prompt outputs: text
"""
    skill_md = """---
name: text-skill
description: text
---
# text-skill

## 执行流程
```bash
python scripts/write.py '{"topic":"{{topic}}","extra":"{{extra}}"}'
```
"""
    results = _check_skill_md_contract(skill_md, blueprint)
    failed_ids = {result.id for result in results if not result.passed}

    assert "command_block.skillplan_inputs.exact" in failed_ids


def test_skill_md_can_delegate_script_command_to_reference():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: delegated-skill
- scripts/: `scripts/write.py`
  scripts/write.py role: text_generator inputs: topic outputs: text
- references/: `references/text-generation.md`
"""
    skill_md = """---
name: delegated-skill
description: delegated
---
# delegated-skill

## 执行流程
1. 读取 `references/text-generation.md` 中的执行步骤和命令模板。
2. 根据 reference 的 command 运行 `scripts/write.py`。

## 参考资料
- `references/text-generation.md`: 定义 text_generator 的命令、输入输出和约束。
"""
    results = _check_skill_md_contract(skill_md, blueprint)
    execution_result = next(result for result in results if result.id == "skill_md.script_command.exists")

    assert execution_result.passed


def test_reference_contract_requires_subtask_contract_sections():
    from backend.routers.creator import _check_reference_file_contract

    content = """
## 规范
按照主题生成文本。

## 示例
输入 topic=猫，输出猫故事。

## 反例
不要输出图片。

## 约束
不得生成 PDF。这里补充足够多的文字以满足最低长度要求，确保文档不是占位符，而是可以指导子任务执行的规则集合。
"""
    results = _check_reference_file_contract("references/text-generation.md", content, purpose="text_generator 子任务执行参考")
    failed_ids = {result.id for result in results if not result.passed}

    assert "reference.subtask_contract_sections" in failed_ids
