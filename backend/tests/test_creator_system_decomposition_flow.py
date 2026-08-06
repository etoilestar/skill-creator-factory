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
        if "Review an Interface Intent Plan" in system:
            calls.append("interface_semantic_review")
            return json.dumps({"passed": True, "issues": []})
        if "source_path" in system or "Return exactly one strict JSON object" in system:
            calls.append("endpoint_planner")
            endpoint_payloads.append(payload)
            obligation = payload["obligation"]
            if obligation["kind"] == "platform_to_script":
                assert "source_path" in system
                assert "never a script path" in system
                assert "Use []" in system
            if obligation["kind"] == "platform_to_script":
                return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
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
    assert calls[6] == "interface_semantic_review"
    assert calls.count("endpoint_planner") == 3


@pytest.mark.asyncio
async def test_bind_plan_repairs_overcomplete_interface_once(monkeypatch):
    initial_plan = {"interfaces": [{"interface_id": "I0003"}]}
    repaired_plan = {"interfaces": [{"interface_id": "I0002"}]}
    frozen_items = [
        {"target_file": "scripts/source.py"},
        {"target_file": "scripts/target.py"},
    ]
    final_edges = [
        {"from_node": "platform_input_node", "to_node": "scripts/source.py"},
        {"from_node": "scripts/source.py", "to_node": "scripts/target.py"},
        {"from_node": "scripts/target.py", "to_node": "platform_output_node"},
    ]
    expansion_plans = []
    repair_calls = []
    planning_calls = []

    monkeypatch.setattr(
        api,
        "_frozen_function_items_from_blueprint",
        lambda **_kwargs: frozen_items,
    )

    async def plan_interfaces(**_kwargs):
        planning_calls.append(_kwargs)
        return initial_plan

    async def expand_graph(**kwargs):
        expansion_plans.append(kwargs["interface_plan"])
        if len(expansion_plans) == 1:
            raise api.ResponsibilityGraphExpansionError(
                "interface has no remaining unbound target input",
                code="interface_plan_overcomplete",
                details={
                    "interface_id": "I0003",
                    "obligation_id": "O0003",
                    "kind": "script_to_script",
                    "source_member": "scripts/source.py",
                    "target_member": "scripts/target.py",
                    "reason": "no_remaining_target_endpoint",
                },
            )
        return final_edges

    async def repair_interfaces(**kwargs):
        repair_calls.append(kwargs)
        error = kwargs["validation_errors"][0]
        assert error["code"] == "interface_plan_overcomplete"
        assert error["details"]["interface_id"] == "I0003"
        assert "Remove or adjust only the interface identified by interface_id" in error["instruction"]
        assert kwargs["affected_members"] == ["scripts/source.py", "scripts/target.py"]
        assert kwargs["missing_platform_output_fields"] == []
        assert kwargs["system_requirements"] == planning_calls[0]["system_requirements"]
        return repaired_plan

    async def creator_model(*_args, **_kwargs):
        raise AssertionError("endpoint model should be handled by the expansion mock")

    monkeypatch.setattr(api, "plan_function_item_interfaces", plan_interfaces)
    monkeypatch.setattr(api, "expand_responsibility_graph", expand_graph)
    monkeypatch.setattr(api, "repair_interface_intents", repair_interfaces)
    monkeypatch.setattr(api, "complete_creator_role_once", creator_model)

    result = await api._bind_executable_responsibility_plan(
        request=_request(),
        current_planner_result={"internal_blueprint_text": _blueprint()},
        planner_model="p",
        allowed_function_item_targets=["scripts/source.py", "scripts/target.py"],
        requirement_allocations=[_allocation("R-system", [])],
        requirement_channels={"R-system": "direct"},
    )

    assert len(repair_calls) == 1
    assert expansion_plans == [initial_plan, repaired_plan]
    assert result["responsibility_edges"] == final_edges
    assert planning_calls[0]["system_requirements"] == [_allocation("R-system", [])]


@pytest.mark.asyncio
async def test_graph_revalidation_failure_is_wrapped_without_third_attempt(monkeypatch):
    plan_value = {"interfaces": []}
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: [])
    monkeypatch.setattr(api, "plan_function_item_interfaces", lambda **_kwargs: _async_value(plan_value))
    attempts = 0

    async def expand_graph(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise api.ResponsibilityGraphExpansionError(
            "still incomplete", code="interface_plan_incomplete",
            details={"uncovered_inputs": []},
        )

    async def repair(**_kwargs):
        return plan_value

    monkeypatch.setattr(api, "expand_responsibility_graph", expand_graph)
    monkeypatch.setattr(api, "repair_interface_intents", repair)
    with pytest.raises(api.InterfaceIntentPlanError) as raised:
        await api._bind_executable_responsibility_plan(
            request=_request(), current_planner_result={"internal_blueprint_text": _blueprint()},
            planner_model="p", allowed_function_item_targets=[],
        )
    assert attempts == 2
    assert raised.value.code == "interface_semantic_repair_failed"
    assert raised.value.details["original_graph_error"]["code"] == "interface_plan_incomplete"
    assert raised.value.details["remaining_graph_error"]["code"] == "interface_plan_incomplete"


@pytest.mark.asyncio
async def test_unknown_graph_error_bypasses_semantic_repair(monkeypatch):
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: [])
    monkeypatch.setattr(api, "plan_function_item_interfaces", lambda **_kwargs: _async_value({"interfaces": []}))
    repair_called = False

    async def expand_graph(**_kwargs):
        raise api.ResponsibilityGraphExpansionError("cycle", code="cycle_error", details={})

    async def repair(**_kwargs):
        nonlocal repair_called
        repair_called = True

    monkeypatch.setattr(api, "expand_responsibility_graph", expand_graph)
    monkeypatch.setattr(api, "repair_interface_intents", repair)
    with pytest.raises(api.ResponsibilityGraphExpansionError) as raised:
        await api._bind_executable_responsibility_plan(
            request=_request(), current_planner_result={"internal_blueprint_text": _blueprint()},
            planner_model="p", allowed_function_item_targets=[],
        )
    assert raised.value.code == "cycle_error"
    assert repair_called is False


async def _async_value(value):
    return value
