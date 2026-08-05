import json

import pytest

from backend.services.creator import api


def _request(**kwargs):
    data = {"user_request": "complete user goal", "human_feedback": "", "model": None}
    data.update(kwargs)
    return api.PreparePlanRequest(**data)


def _script_block(path: str, *, purpose: str, inputs: list[str], outputs: list[str]) -> str:
    return (
        f"- path: `{path}`\n"
        "  role: generic_script\n"
        f"  purpose: {purpose}\n"
        f"  inputs: [{', '.join(inputs)}]\n"
        f"  outputs: [{', '.join(outputs)}]\n"
        "  dependencies: []\n"
        "  required_capabilities: []\n"
        "  forbidden_capabilities: []\n"
        "  references: []\n"
        "  constraints: []"
    )


def _blueprint() -> str:
    return f"""## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: demo-skill
### I/O 契约
- **输入**: runtime text
- **输出**: text
- **触发词**: process
### 目录结构
[当前 Skill 根目录]
├── SKILL.md
└── scripts
    ├── a.py
    └── b.py
### 工作流逻辑
1. A 完成第一个原子执行子目标
2. B 使用 A 的结果完成第二个原子执行子目标
### SkillPlan / 文件职责计划
- path: `SKILL.md`
  role: skill_overview
  inputs: []
  outputs: []
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: []
  references: []
  constraints: []
{_script_block('scripts/a.py', purpose='atomic subgoal 1', inputs=['input_1'], outputs=['output_1'])}
{_script_block('scripts/b.py', purpose='atomic subgoal 2', inputs=['input_1'], outputs=['output_1'])}
### 宿主执行方式
- **直接回答**: 否
- **需要脚本/命令**: 是
- **禁止隐式执行**: 遵守
- **执行后回答**: 返回 text
### 资源清单
- [ ] 无
"""


def _allocation(requirement_id: str, owners: list[str]) -> dict:
    return {
        "requirement_id": requirement_id,
        "requirement": f"requirement-{requirement_id}",
        "owners": owners,
        "evidence": {"responsibility": "planner evidence", "outputs": [], "capabilities": []},
    }


@pytest.mark.asyncio
async def test_prepare_main_path_reconciles_decomposition_then_interface_binds_graph(monkeypatch):
    blueprint = _blueprint()
    calls: list[str] = []
    interface_payloads: list[dict] = []
    endpoint_payloads: list[dict] = []
    review_responses = iter([
        {
            "passed": False,
            "issues": [{
                "issue_type": "requirement_uncovered",
                "requirement_id": "R2",
                "blocking_now": True,
                "evidence_stage": "blueprint",
                "repair_scope": "allocation",
                "affected_targets": ["scripts/b.py"],
                "evidence": [{"source": "requirement_allocations", "target": "R2", "field": "owners", "observed": []}],
                "expected_fact": "R2 should be owned by an existing FunctionItem after reconciliation",
                "reason": "owner missing before reconciliation",
                "repair_guidance": "assign an existing owner",
            }],
            "deferred_checks": [],
        },
        {"passed": True, "issues": [], "deferred_checks": []},
    ])

    async def initial_planner(_messages, *_args, **_kwargs):
        calls.append("blueprint_planner")
        return json.dumps({
            "status": "ready",
            "clarifying_questions": [],
            "review_summary": {},
            "internal_blueprint_text": blueprint,
            "skill_name": "demo-skill",
            "blockers": [],
        })

    async def creator_model(messages, role, fallback_model=None):
        system = str(messages[0].get("content") or "")
        payload = json.loads(messages[-1]["content"])
        if "Skill Creator 模式" in system:
            calls.append("blueprint_planner")
            return json.dumps({
                "status": "ready",
                "clarifying_questions": [],
                "review_summary": {},
                "internal_blueprint_text": blueprint,
                "skill_name": "demo-skill",
                "blockers": [],
            })
        if "requirement coverage projection" in system:
            calls.append("requirement_allocation")
            return json.dumps({
                "requirement_allocations": [_allocation("R1", ["scripts/a.py"]), _allocation("R2", [])],
                "requirement_channels": {"R1": "executable", "R2": "executable"},
            })
        if "semantic coverage Reviewer" in system:
            calls.append("semantic_review")
            return json.dumps(next(review_responses))
        if "reconciling a requirement allocation exactly once" in system:
            calls.append("allocation_reconciliation")
            return json.dumps({
                "requirement_allocations": [_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/b.py"])],
                "requirement_channels": {"R1": "executable", "R2": "executable"},
            })
        if "planning semantic interfaces between already-frozen executable FunctionItems" in system:
            calls.append("interface_intent_planner")
            interface_payloads.append(payload)
            return json.dumps({
                "interfaces": [
                    {"interface_id": "I0001", "kind": "platform_to_member", "goal": "runtime input", "target_member": "scripts/a.py"},
                    {"interface_id": "I0002", "kind": "member_to_member", "goal": "handoff", "source_member": "scripts/a.py", "target_member": "scripts/b.py"},
                    {"interface_id": "I0003", "kind": "member_to_platform", "goal": "final output", "source_member": "scripts/b.py"},
                ]
            })
        if "Select endpoint IDs only for this declared interface intent" in system:
            calls.append("endpoint_planner")
            endpoint_payloads.append(payload)
            obligation = payload["obligation"]
            if obligation["kind"] == "platform_to_script":
                return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
            if obligation["kind"] == "script_to_script":
                return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})
            slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        raise AssertionError(f"unexpected model call for role={role}: {system[:120]}")

    monkeypatch.setattr(api, "complete_chat_once", initial_planner)
    monkeypatch.setattr(api, "complete_creator_role_once", creator_model)

    result = await api._generate_internal_blueprint_or_questions(_request())

    assert result["status"] == "ready"
    assert [item["target_file"] for item in result["function_items"]] == ["scripts/a.py", "scripts/b.py"]
    assert [item["inputs"] for item in result["function_items"]] == [["input_1"], ["input_1"]]
    assert [item["outputs"] for item in result["function_items"]] == [["output_1"], ["output_1"]]
    assert all(item["purpose"] for item in result["function_items"])
    assert result["function_items"][0]["purpose"] != result["function_items"][1]["purpose"]
    assert [edge["to_node"] for edge in result["responsibility_edges"]] == ["scripts/a.py", "scripts/b.py", "platform_output_node"]
    assert interface_payloads
    assert {item["target_file"] for item in interface_payloads[0]["function_items"]} == {"scripts/a.py", "scripts/b.py"}
    assert not {"subsystems", "subsystem_links", "members"} & set(interface_payloads[0])
    assert endpoint_payloads
    assert all("binding_candidates" not in payload for payload in endpoint_payloads)
    assert all("legacy_" + "goal_expansion" not in json.dumps(payload) for payload in endpoint_payloads)
    assert calls[:5] == [
        "blueprint_planner",
        "requirement_allocation",
        "semantic_review",
        "allocation_reconciliation",
        "semantic_review",
    ]
    assert calls[5] == "interface_intent_planner"
    assert calls.count("endpoint_planner") == 3
