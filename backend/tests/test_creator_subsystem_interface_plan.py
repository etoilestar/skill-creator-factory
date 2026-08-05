import json

import pytest

from backend.services.creator.responsibility_graph_expansion import expand_responsibility_graph
from backend.services.creator.subsystem_interface_plan import (
    SubsystemInterfacePlanError,
    build_graph_obligations_from_subsystems,
    validate_subsystem_interface_plan,
)
from backend.services.platform_io_contract import build_platform_io_contract


def item(node, inputs, outputs):
    return {"target_file": node, "role": "script", "purpose": f"purpose-{node}", "inputs": inputs, "outputs": outputs, "required_capabilities": [], "constraints": [], "default_values": {}}


def serial_plan():
    return {
        "subsystems": [
            {"subsystem_id": "S0001", "goal": "first subgoal", "members": ["scripts/a.py"], "internal_interfaces": [], "external_inputs": [{"interface_id": "IF0001", "goal": "receive runtime data", "consumer_member": "scripts/a.py", "source_scope": "platform"}], "external_outputs": [{"interface_id": "IF0002", "goal": "provide intermediate data", "producer_member": "scripts/a.py", "target_scope": "subsystem"}]},
            {"subsystem_id": "S0002", "goal": "second subgoal", "members": ["scripts/b.py"], "internal_interfaces": [], "external_inputs": [{"interface_id": "IF0003", "goal": "receive intermediate data", "consumer_member": "scripts/b.py", "source_scope": "subsystem"}], "external_outputs": [{"interface_id": "IF0004", "goal": "provide final data", "producer_member": "scripts/b.py", "target_scope": "platform"}]},
        ],
        "subsystem_links": [{"link_id": "SL0001", "source_subsystem_id": "S0001", "source_interface_id": "IF0002", "target_subsystem_id": "S0002", "target_interface_id": "IF0003", "transfer_goal": "move intermediate data"}],
    }


def test_validate_subsystem_plan_rejects_unknown_member_and_extra_fields():
    items = [item("scripts/a.py", ["x"], ["y"])]
    plan = {"subsystems": [{"subsystem_id": "S0001", "goal": "g", "members": ["scripts/missing.py"], "internal_interfaces": [], "external_inputs": [], "external_outputs": [], "extra": True}], "subsystem_links": []}
    with pytest.raises(SubsystemInterfacePlanError) as raised:
        validate_subsystem_interface_plan(plan=plan, function_items=items)
    assert raised.value.code == "invalid_subsystem_protocol"


def test_validate_subsystem_plan_rejects_uncovered_and_scope_mismatches():
    items = [item("scripts/a.py", ["x"], ["y"]), item("scripts/b.py", ["x"], ["y"])]
    plan = {"subsystems": [{"subsystem_id": "S0001", "goal": "g", "members": ["scripts/a.py"], "internal_interfaces": [], "external_inputs": [], "external_outputs": []}], "subsystem_links": []}
    with pytest.raises(SubsystemInterfacePlanError) as raised:
        validate_subsystem_interface_plan(plan=plan, function_items=items)
    assert raised.value.code == "uncovered_function_item"

    bad = serial_plan()
    bad["subsystems"][1]["external_inputs"][0]["source_scope"] = "platform"
    with pytest.raises(SubsystemInterfacePlanError) as raised:
        validate_subsystem_interface_plan(plan=bad, function_items=items)
    assert raised.value.code == "invalid_external_input_scope_link"


def test_build_graph_obligations_preserves_declared_interface_scope():
    plan = validate_subsystem_interface_plan(plan=serial_plan(), function_items=[item("scripts/a.py", ["raw"], ["mid"]), item("scripts/b.py", ["mid"], ["done"])])
    obligations = build_graph_obligations_from_subsystems(subsystem_plan=plan)
    assert [ob["kind"] for ob in obligations] == ["platform_to_script", "script_to_platform", "script_to_script"]
    link_obligation = obligations[2]
    assert link_obligation["source_member"] == "scripts/a.py"
    assert link_obligation["target_member"] == "scripts/b.py"
    assert link_obligation["allowed_source_scope"] == "declared_source_member"


@pytest.mark.asyncio
async def test_subsystem_expansion_limits_endpoint_selection_to_declared_members():
    items = [item("scripts/a.py", ["raw"], ["mid"]), item("scripts/b.py", ["mid"], ["done"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        if obligation["kind"] == "script_to_platform":
            text_slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == "text")
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": text_slot["slot_id"]})
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=model, subsystem_plan=serial_plan())
    assert [(edge["from_node"], edge["to_node"]) for edge in edges] == [("platform_input_node", "scripts/a.py"), ("scripts/b.py", "platform_output_node"), ("scripts/a.py", "scripts/b.py")]
    script_to_script_call = next(call for call in calls if call["obligation"]["kind"] == "script_to_script")
    assert [value["port_id"] for value in script_to_script_call["source_member_outputs"]] == ["mid"]
    assert [value["port_id"] for value in script_to_script_call["target_member_inputs"]] == ["mid"]
    assert "script_outputs" not in script_to_script_call
