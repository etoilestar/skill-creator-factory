"""Patch snippets for backend/routers/creator.py.

把这些函数/类合并到现有 creator.py 中，不建议直接整文件覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field

from backend.services.skill_contract import WorkflowContract, StepContract
from backend.services.skill_contract_validator import validate_workflow_contract, validate_stdout_against_output_schema, parse_stdout_json_object


class AnalyzeBlueprintResponseContractMixin(BaseModel):
    """把这些字段合并到现有 AnalyzeBlueprintResponse。"""
    workflow_contract: dict[str, Any] | None = None
    resource_manifest: list[dict[str, Any]] = Field(default_factory=list)


class FileSpecOutContractMixin(BaseModel):
    """把这些字段合并到现有 FileSpecOut。"""
    step_id: Optional[str] = None
    contract_inputs: dict[str, Any] = Field(default_factory=dict)
    contract_outputs: dict[str, Any] = Field(default_factory=dict)
    foreach: Optional[dict[str, Any]] = None
    collect: list[dict[str, Any]] = Field(default_factory=list)
    resource_metadata: dict[str, Any] = Field(default_factory=dict)


def workflow_contract_from_blueprint_payload(payload: dict[str, Any]) -> WorkflowContract:
    """解析模型/前端返回的 workflow_contract 字段。

    Creator 应在 analyze-blueprint 阶段调用此函数，拿到 contract 后先静态校验。
    """
    raw = payload.get("workflow_contract") or payload.get("contract") or {}
    if not isinstance(raw, dict):
        raise ValueError("workflow_contract must be a JSON object")
    contract = WorkflowContract.from_raw(raw)
    issues = validate_workflow_contract(contract)
    errors = [x.to_dict() for x in issues if x.severity == "error"]
    if errors:
        raise ValueError("WorkflowContract 校验失败: " + json.dumps(errors, ensure_ascii=False))
    return contract


def skill_plan_entry_dict_from_contract_step(step: StepContract) -> dict[str, Any]:
    """在 _skill_plan_entry_for_file 中优先使用此结果构造 SkillPlanEntry。"""
    return {
        "path": step.script_path,
        "role": step.role,
        "inputs": list(step.inputs.keys()),
        "outputs": list(step.outputs.keys()),
        "default_values": step.default_values,
        "dependencies": step.dependencies,
        "required_capabilities": step.required_capabilities,
        "command_template": step.command_template or command_template_from_contract_step(step),
    }


def command_template_from_contract_step(step: StepContract) -> str:
    payload = {key: "{{" + key + "}}" for key in step.inputs.keys()}
    return "python " + step.script_path + " " + json.dumps(payload, ensure_ascii=False)


def build_script_file_contract_text_from_step(step: StepContract) -> str:
    """替换/增强 _build_script_file_contract_text 的核心文本。"""
    outputs_schema = {
        name: {
            "type": spec.type,
            "required": spec.required,
            "min_length": spec.min_length,
            "min_items": spec.min_items,
            "item_required": spec.item_required,
            "path_must_exist": spec.path_must_exist,
        }
        for name, spec in step.outputs.items()
    }
    inputs_schema = {
        name: {
            "type": spec.type,
            "required": spec.required,
            "default": spec.default,
            "source": spec.source,
        }
        for name, spec in step.inputs.items()
    }
    return f"""
[WorkflowContract Step]
script_path: {step.script_path}
role: {step.role}
inputs_schema:
{json.dumps(inputs_schema, ensure_ascii=False, indent=2)}
outputs_schema:
{json.dumps(outputs_schema, ensure_ascii=False, indent=2)}
required_capabilities: {json.dumps(step.required_capabilities, ensure_ascii=False)}
dependencies: {json.dumps(step.dependencies, ensure_ascii=False)}
command_template:
{step.command_template or command_template_from_contract_step(step)}

硬性要求：
1. 只能读取 inputs_schema 中声明的输入字段。
2. stdout 必须且只能 print 一个 JSON object。
3. required output 必须存在，字段名不能改。
4. output 类型必须满足 outputs_schema。
5. array output 如果 min_items>=1，必须返回非空列表。
6. 如果 array item_required 非空，每个 item 必须包含这些字段。
7. file_path/file_paths 输出必须指向真实创建的文件。
8. 不要修改 SKILL.md、不要修改命令 JSON keys、不要新增上游必填输入。
""".strip()


def build_reference_file_contract_text_v2(file_path: str, purpose: str = "") -> str:
    """替换 _build_reference_file_contract_text 的核心原则。"""
    return f"""
你正在生成 reference 文件：{file_path}

reference 是参考资料/规范/示例，不是子功能，不执行任务。
它不能重新定义 role、inputs、outputs、required_capabilities、command_template。
它应该提供：
1. 领域知识或风格规范
2. 输出质量标准
3. few-shot 示例
4. 禁止事项和边界条件

文件顶部必须包含 frontmatter metadata：
---
summary: 一句话说明这个 reference 的用途
keywords: [关键词1, 关键词2]
applies_to_roles: [text_generator/image_generator/pdf_builder/...]
applies_to_steps: [scripts/xxx.py]
when_to_read: 什么时候读取
load_policy: on_demand
---

当前用途：{purpose}
""".strip()


def validate_trial_stdout_json_v2(
    *,
    stdout: str,
    step: StepContract,
    execution_root: Path,
    downstream_requirements: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """替换旧 _validate_trial_stdout_json 的核心逻辑。"""
    payload, parse_issue = parse_stdout_json_object(stdout)
    if parse_issue:
        parse_issue.step_id = step.id
        parse_issue.script_path = step.script_path
        return [parse_issue.to_dict()]
    assert payload is not None
    issues = validate_stdout_against_output_schema(
        payload,
        step,
        execution_root=execution_root,
        downstream_requirements=downstream_requirements or {},
    )
    return [x.to_dict() for x in issues]
