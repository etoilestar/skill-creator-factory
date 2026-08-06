import json

import pytest

from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError,
    expand_responsibility_graph,
)
from backend.services.creator.function_item_interface_plan import (
    InterfaceIntentPlanError,
    build_graph_obligations_from_interfaces,
    plan_function_item_interfaces,
    repair_interface_intents,
    build_interface_repair_scope,
    collect_interface_plan_validation_issues,
    merge_interface_validation_issues,
    repair_interface_plan_semantically,
    review_interface_plan_semantically,
    validate_interface_repair_scope,
    validate_interface_intent_plan,
    _interface_plan_prompt,
)
from backend.services.platform_io_contract import build_platform_io_contract


def item(node, inputs, outputs):
    return {"target_file": node, "role": "script", "purpose": f"purpose-{node}", "inputs": inputs, "outputs": outputs, "required_capabilities": [], "constraints": [], "default_values": {}}


def plan(*interfaces):
    return {"interfaces": list(interfaces)}


def p2m(interface_id, target, goal="runtime input"):
    return {"interface_id": interface_id, "kind": "platform_to_member", "goal": goal, "target_member": target}


def m2m(interface_id, source, target, goal="member result"):
    return {"interface_id": interface_id, "kind": "member_to_member", "goal": goal, "source_member": source, "target_member": target}


def m2p(interface_id, source, goal="platform output"):
    return {"interface_id": interface_id, "kind": "member_to_platform", "goal": goal, "source_member": source}


def fenced_json(value):
    return "```json\n" + json.dumps(value) + "\n```"


def test_interface_plan_rejects_unknown_member_self_connection_and_extra_fields():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    with pytest.raises(InterfaceIntentPlanError) as raised:
        validate_interface_intent_plan(plan=plan({**p2m("I0001", "scripts/missing.py"), "extra": True}), function_items=items)
    assert raised.value.code == "invalid_interface_protocol"

    with pytest.raises(InterfaceIntentPlanError) as raised:
        validate_interface_intent_plan(plan=plan(m2m("I0001", "scripts/a.py", "scripts/a.py")), function_items=items)
    assert raised.value.code == "interface_self_connection"


def test_build_graph_obligations_from_interfaces_preserves_member_scope():
    interface_plan = validate_interface_intent_plan(
        plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/b.py")),
        function_items=[item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"])],
    )
    obligations = build_graph_obligations_from_interfaces(interface_plan=interface_plan)
    assert [ob["kind"] for ob in obligations] == ["platform_to_script", "script_to_script", "script_to_platform"]
    assert obligations[1]["source_member"] == "scripts/a.py"
    assert obligations[1]["target_member"] == "scripts/b.py"
    assert "subsystem_id" not in obligations[1]


async def scripted_endpoint_model(messages, _model):
    payload = json.loads(messages[-1]["content"])
    obligation = payload["obligation"]
    if obligation["kind"] == "platform_to_script":
        return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
    if obligation["kind"] == "script_to_platform":
        slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
    return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})


@pytest.mark.asyncio
async def test_single_function_item_platform_to_member_to_platform_graph_valid():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=scripted_endpoint_model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert [(edge["from_node"], edge["to_node"]) for edge in edges] == [("platform_input_node", "scripts/a.py"), ("scripts/a.py", "platform_output_node")]


@pytest.mark.asyncio
async def test_three_serial_function_items_use_scoped_endpoint_binding():
    items = [item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"]), item("scripts/c.py", ["input_1"], ["output_1"])]
    calls = []

    async def model(messages, model_name):
        calls.append(json.loads(messages[-1]["content"]))
        return await scripted_endpoint_model(messages, model_name)

    await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2m("I0003", "scripts/b.py", "scripts/c.py"), m2p("I0004", "scripts/c.py")))
    member_call = next(call for call in calls if call["obligation"]["interface_id"] == "I0002")
    assert [value["port_id"] for value in member_call["source_member_outputs"]] == ["output_1"]
    assert [value["port_id"] for value in member_call["target_member_inputs"]] == ["input_1"]
    assert "script_outputs" not in member_call


@pytest.mark.asyncio
async def test_branch_converge_multi_input_and_multi_platform_output_shapes():
    items = [item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"]), item("scripts/c.py", ["input_1", "input_2"], ["output_1", "output_2"])]
    platform = {"platform_skill_boundary": {"input_envelope_fields": ["fields", "options"], "final_output_fields": ["text", "pdf_path"], "required_final_output_fields": ["text", "pdf_path"]}}
    terminal_fields = iter(["text", "pdf_path"])

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
        if obligation["kind"] == "script_to_platform":
            field = next(terminal_fields)
            slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == field)
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=platform, planner_model="p", goal_context={}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), p2m("I0002", "scripts/b.py"), m2m("I0003", "scripts/a.py", "scripts/c.py"), m2m("I0004", "scripts/b.py", "scripts/c.py"), m2p("I0005", "scripts/c.py"), m2p("I0006", "scripts/c.py")))
    assert len([edge for edge in edges if edge["to_node"] == "platform_output_node"]) == 2


@pytest.mark.asyncio
async def test_same_output_can_feed_member_and_platform_when_interfaces_declare_both():
    items = [item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"])]
    terminal_fields = iter(["text", "pdf_path"])

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
        if obligation["kind"] == "script_to_platform":
            field = next(terminal_fields)
            slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == field)
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/a.py"), m2p("I0004", "scripts/b.py")))
    assert ("scripts/a.py", "platform_output_node") in {(edge["from_node"], edge["to_node"]) for edge in edges}
    assert ("scripts/a.py", "scripts/b.py") in {(edge["from_node"], edge["to_node"]) for edge in edges}


@pytest.mark.asyncio
async def test_plan_and_endpoint_selection_accept_single_json_fence_and_protocol_reformat():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    valid_plan = plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py"))
    responses = iter([json.dumps({"interfaces": [{**p2m("I0001", "scripts/a.py"), "extra": "bad"}, m2p("I0002", "scripts/a.py")]}), fenced_json(valid_plan)])

    async def planner_model(_messages, _model):
        return next(responses)

    assert await plan_function_item_interfaces(original_user_goal="g", frozen_function_items=items, planner_model="p", model_call=planner_model) == valid_plan

    async def endpoint_model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if payload["obligation"]["kind"] == "platform_to_script":
            return fenced_json({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
        slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == "text")
        return fenced_json({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=endpoint_model, interface_plan=valid_plan)
    assert edges[-1]["to_node"] == "platform_output_node"


@pytest.mark.asyncio
async def test_missing_input_and_missing_platform_output_raise_interface_or_terminal_errors():
    items = [item("scripts/a.py", ["input_1", "input_2"], ["output_1"])]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=scripted_endpoint_model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert raised.value.code == "interface_plan_incomplete"

    platform = {"platform_skill_boundary": {"input_envelope_fields": ["fields"], "final_output_fields": ["text", "pdf_path"], "required_final_output_fields": ["text", "pdf_path"]}}
    single = [item("scripts/a.py", [], ["output_1"])]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=single, platform_contract=platform, planner_model="p", goal_context={}, model_call=scripted_endpoint_model, interface_plan=plan(m2p("I0001", "scripts/a.py")))
    assert raised.value.code == "interface_plan_incomplete"


@pytest.mark.asyncio
async def test_repair_interface_intents_returns_validated_repaired_plan():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    repaired = plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py"))

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        assert payload["current_interface_plan"] == {"interfaces": []}
        assert payload["affected_members"] == ["scripts/a.py"]
        assert payload["validation_errors"][0]["code"] == "interface_plan_incomplete"
        return json.dumps(repaired)

    assert await repair_interface_intents(original_user_goal="g", frozen_function_items=items, current_interface_plan={"interfaces": []}, validation_errors=[{"code": "interface_plan_incomplete"}], affected_members=["scripts/a.py"], missing_platform_output_fields=[], planner_model="p", model_call=model) == repaired


def allocation(requirement_id, owners):
    return {"requirement_id": requirement_id, "requirement": f"requirement-{requirement_id}", "owners": owners, "evidence": {"responsibility": "r", "outputs": [], "capabilities": []}}


def test_frozen_function_item_subgoal_validation_requires_purpose_and_executable_owners():
    from backend.services.creator.api import PreparePlanProtocolError, validate_frozen_function_item_structure, validate_final_executable_requirement_ownership

    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    summary = validate_frozen_function_item_structure(function_items=items, requirement_allocations=[allocation("R1", ["scripts/a.py"])])
    assert summary["decomposition_valid"] is True
    assert summary["targets"] == ["scripts/a.py"]

    bad_purpose = [{**items[0], "purpose": ""}]
    with pytest.raises((PreparePlanProtocolError, ValueError)):
        validate_frozen_function_item_structure(function_items=bad_purpose, requirement_allocations=[allocation("R1", ["scripts/a.py"])])

    with pytest.raises(PreparePlanProtocolError):
        validate_final_executable_requirement_ownership(requirement_allocations=[allocation("R1", [])], requirement_channels={"R1": "executable"}, allowed_owner_targets=["scripts/a.py"])


def test_interface_planner_payload_is_compact_and_uses_existing_function_items_only():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    captured = {}

    async def model(messages, _model):
        captured.update(json.loads(messages[-1]["content"]))
        return json.dumps(plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))

    import asyncio
    asyncio.run(plan_function_item_interfaces(original_user_goal="goal", frozen_function_items=items, requirement_allocations=[allocation("R1", ["scripts/a.py"]), allocation("R2", [])], requirement_channels={"R1": "executable", "R2": "direct"}, planner_model="p", model_call=model))
    assert set(captured) == {"system_goal", "skill_name", "function_items", "executable_requirement_allocations", "requirement_channels", "unowned_system_requirements", "platform_contract"}
    assert captured["function_items"][0]["target_file"] == "scripts/a.py"
    assert captured["function_items"][0]["required_inputs"] == ["input_1"]
    assert captured["function_items"][0]["defaulted_inputs"] == []
    assert [value["requirement_id"] for value in captured["executable_requirement_allocations"]] == ["R1"]
    assert [value["requirement_id"] for value in captured["unowned_system_requirements"]] == ["R2"]


def test_semantic_validator_collects_all_reference_levels_and_self_connections():
    items = [item("scripts/unit_1.py", ["value_a"], ["value_b"]), item("scripts/unit_2.py", ["value_b"], ["value_a"])]
    candidate = plan(
        m2m("I1", "missing_1", "value_b"),
        m2m("I2", "missing_2", "scripts/unit_2.py"),
        m2m("I3", "scripts/unit_1.py", "scripts/unit_1.py"),
    )
    issues = collect_interface_plan_validation_issues(plan=candidate, function_items=items)
    assert [issue["code"] for issue in issues] == [
        "unknown_interface_member", "unknown_interface_member",
        "unknown_interface_member", "interface_self_connection",
    ]
    assert issues[1]["category"] == "reference_scope_error"
    assert all(issue["allowed_scope"] == ["scripts/unit_1.py", "scripts/unit_2.py"] for issue in issues)
    assert not any("correct_source_member" in issue for issue in issues)


@pytest.mark.asyncio
async def test_initial_semantic_errors_are_repaired_once_without_backend_inference():
    items = [item("scripts/unit_1.py", ["value_a"], ["value_b"]), item("scripts/unit_2.py", ["value_b"], ["value_a"])]
    responses = iter([
        json.dumps(plan(m2m("I1", "unknown", "scripts/unit_2.py"), m2m("I2", "scripts/unit_1.py", "scripts/unit_1.py"))),
        json.dumps(plan(m2m("I1", "scripts/unit_1.py", "scripts/unit_2.py"), m2m("I2", "scripts/unit_2.py", "scripts/unit_1.py"))),
    ])
    calls = []

    async def model(messages, _model):
        calls.append(messages)
        return next(responses)

    repaired = await plan_function_item_interfaces(
        original_user_goal="abstract collaboration", frozen_function_items=items,
        planner_model="p", model_call=model,
    )
    assert len(calls) == 2
    repair_payload = json.loads(calls[1][-1]["content"])
    assert len(repair_payload["validation_issues"]) == 2
    assert repaired["interfaces"][0]["source_member"] == "scripts/unit_1.py"


def test_repair_scope_rejects_unrelated_changes_reordering_and_hidden_endpoint_fields():
    before = plan(p2m("I1", "unit_1"), m2p("I2", "unit_2"))
    scope = build_interface_repair_scope([{
        "code": "unknown_interface_member", "category": "reference_error",
        "interface_id": "I1", "details": {},
    }])
    with pytest.raises(InterfaceIntentPlanError) as raised:
        validate_interface_repair_scope(
            before=before,
            after=plan({**m2p("I2", "unit_2"), "endpoint_id": "E1"}, p2m("I1", "unit_1")),
            repair_scope=scope,
        )
    assert raised.value.code == "interface_repair_scope_error"


def test_old_subsystem_module_and_test_are_removed():
    from pathlib import Path

    assert not Path("backend/services/creator/" + "subsystem_" + "interface_plan.py").exists()
    assert not Path("backend/tests/test_creator_" + "subsystem_" + "interface_plan.py").exists()


@pytest.mark.asyncio
async def test_ownerless_executable_requirement_passes_structure_then_fails_final_ownership():
    from backend.services.creator.api import PreparePlanProtocolError, validate_frozen_function_item_structure, validate_final_executable_requirement_ownership

    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    validate_frozen_function_item_structure(function_items=items, requirement_allocations=[allocation("R1", [])])
    with pytest.raises(PreparePlanProtocolError):
        validate_final_executable_requirement_ownership(requirement_allocations=[allocation("R1", [])], requirement_channels={"R1": "executable"}, allowed_owner_targets=["scripts/a.py"])


@pytest.mark.asyncio
async def test_interface_protocol_repair_failure_has_specific_code():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    responses = iter([
        json.dumps({"interfaces": [{**p2m("I0001", "scripts/a.py"), "extra": True}]}),
        json.dumps({"still": "wrong"}),
    ])

    async def model(_messages, _model):
        return next(responses)

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await plan_function_item_interfaces(original_user_goal="goal", frozen_function_items=items, planner_model="p", model_call=model)
    assert raised.value.code == "interface_plan_protocol_repair_failed"


@pytest.mark.asyncio
async def test_interface_protocol_repair_wraps_second_invalid_json():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    responses = iter([
        "{not-json",
        "{still-not-json",
    ])

    async def model(_messages, _model):
        return next(responses)

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await plan_function_item_interfaces(original_user_goal="goal", frozen_function_items=items, planner_model="p", model_call=model)
    assert raised.value.code == "interface_plan_protocol_repair_failed"
    assert raised.value.details["repair_error"]["code"] == "invalid_interface_plan_json"


def test_interface_and_repair_prompts_define_one_interface_per_transfer():
    prompt = " ".join(_interface_plan_prompt().lower().split())
    assert "each interface object as exactly one source endpoint connected to exactly one target endpoint" in prompt
    assert "do not combine multiple independently required target inputs" in prompt
    assert "multiple member_to_member interfaces between the same source_member and target_member" in prompt
    assert "source outputs are reusable" in prompt
    assert "do not determine the number of interfaces from the number of source outputs" in prompt
    assert "a structured value transferred into one target input remains one logical transfer regardless of how many internal fields" in prompt
    assert "required_inputs require incoming transfer coverage" in prompt
    assert "defaulted_inputs do not require an interface" in prompt

    captured = {}
    items = [
        item("scripts/a.py", ["runtime_input"], ["first", "second", "third"]),
        item("scripts/b.py", ["first", "second", "third"], ["result"]),
    ]
    repaired = plan(
        p2m("I0001", "scripts/a.py"),
        m2m("I0002", "scripts/a.py", "scripts/b.py", goal="transfer one"),
        m2m("I0003", "scripts/a.py", "scripts/b.py", goal="transfer two"),
        m2m("I0004", "scripts/a.py", "scripts/b.py", goal="transfer three"),
        m2p("I0005", "scripts/b.py"),
    )

    async def model(messages, _model):
        captured["system"] = " ".join(messages[0]["content"].lower().split())
        captured["payload"] = json.loads(messages[-1]["content"])
        return json.dumps(repaired)

    import asyncio

    result = asyncio.run(
        repair_interface_intents(
            original_user_goal="g",
            frozen_function_items=items,
            current_interface_plan=plan(
                p2m("I0001", "scripts/a.py"),
                m2m("I0002", "scripts/a.py", "scripts/b.py", goal="transfer one"),
                m2p("I0005", "scripts/b.py"),
            ),
            validation_errors=[{
                "code": "interface_plan_incomplete",
                "details": {
                    "uncovered_inputs": [
                        {"target": "scripts/b.py", "input_id": "second"},
                        {"target": "scripts/b.py", "input_id": "third"},
                    ]
                },
            }, {
                "code": "interface_plan_overcomplete",
                "details": {
                    "interface_id": "I0006",
                    "obligation_id": "O0006",
                    "kind": "script_to_script",
                    "reason": "no_remaining_target_endpoint",
                },
            }],
            affected_members=["scripts/b.py"],
            missing_platform_output_fields=[],
            planner_model="p",
            model_call=model,
        )
    )

    assert "uncovered_inputs" in captured["system"]
    assert "does not prove that every required target input is covered" in captured["system"]
    assert "do not return the plan unchanged" in captured["system"]
    assert "source endpoints are reusable" in captured["system"]
    assert "interface_plan_overcomplete means only" in captured["system"]
    assert "do not create interfaces solely because a source member exposes additional outputs" in captured["system"]
    assert captured["payload"]["uncovered_inputs"] == [
        {"target": "scripts/b.py", "input_id": "second"},
        {"target": "scripts/b.py", "input_id": "third"},
    ]
    assert captured["payload"]["overcomplete_interfaces"] == [{
        "interface_id": "I0006",
        "obligation_id": "O0006",
        "kind": "script_to_script",
        "reason": "no_remaining_target_endpoint",
    }]
    repeated = [
        iface for iface in result["interfaces"]
        if iface["kind"] == "member_to_member"
        and iface["source_member"] == "scripts/a.py"
        and iface["target_member"] == "scripts/b.py"
    ]
    assert len(repeated) == 3
    assert len({iface["interface_id"] for iface in repeated}) == 3
    assert len({iface["goal"] for iface in repeated}) == 3


def test_affected_members_expand_scope_only_to_directly_related_interfaces():
    current = plan(
        m2m("I1", "scripts/unit_1.py", "scripts/unit_2.py"),
        m2m("I2", "scripts/unit_3.py", "scripts/unit_4.py"),
    )
    issues = [{
        "code": "interface_plan_incomplete", "category": "coverage_error",
        "interface_id": "", "details": {
            "uncovered_inputs": [{"target": "scripts/unit_2.py", "input_id": "value_b"}]
        },
    }]
    scope = build_interface_repair_scope(issues, current)
    assert scope["affected_interface_ids"] == ["I1"]
    validate_interface_repair_scope(
        before=current,
        after=plan(m2m("I1", "scripts/unit_1.py", "scripts/unit_2.py", "adjusted"), current["interfaces"][1]),
        repair_scope=scope,
    )
    with pytest.raises(InterfaceIntentPlanError):
        validate_interface_repair_scope(
            before=current,
            after=plan(current["interfaces"][0], m2m("I2", "scripts/unit_4.py", "scripts/unit_3.py")),
            repair_scope=scope,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("response,underlying", [
    ("{bad", "invalid_interface_plan_json"),
    (json.dumps({"interfaces": [{**p2m("I1", "scripts/unit_1.py"), "extra": True}]}), "invalid_interface_protocol"),
    (json.dumps({"interfaces": [{"interface_id": "I1", "kind": "wrong", "goal": "g"}]}), "invalid_interface_kind"),
    (json.dumps(plan(p2m("I1", "scripts/unit_1.py"), m2p("I1", "scripts/unit_1.py"))), "duplicate_interface_id"),
])
async def test_all_semantic_repair_protocol_failures_are_wrapped(response, underlying):
    items = [item("scripts/unit_1.py", ["value_a"], ["value_b"])]
    calls = 0

    async def model(_messages, _model):
        nonlocal calls
        calls += 1
        return response

    issue = {"code": "interface_self_connection", "category": "direction_error", "interface_id": "I1", "details": {}}
    with pytest.raises(InterfaceIntentPlanError) as raised:
        await repair_interface_plan_semantically(
            original_user_goal="g", frozen_function_items=items,
            current_interface_plan=plan(p2m("I1", "scripts/unit_1.py")),
            validation_issues=[issue], repair_scope=build_interface_repair_scope([issue]),
            planner_model="p", model_call=model,
        )
    assert calls == 1
    assert raised.value.code == "interface_semantic_repair_failed"
    assert raised.value.details["repair_error"]["code"] == underlying


@pytest.mark.asyncio
async def test_duplicate_interface_id_gets_one_protocol_repair_only():
    items = [item("scripts/unit_1.py", ["value_a"], ["value_b"])]
    responses = iter([
        json.dumps(plan(p2m("I1", "scripts/unit_1.py"), m2p("I1", "scripts/unit_1.py"))),
        json.dumps(plan(p2m("I1", "scripts/unit_1.py"), m2p("I2", "scripts/unit_1.py"))),
    ])
    calls = 0

    async def model(_messages, _model):
        nonlocal calls
        calls += 1
        return next(responses)

    result = await plan_function_item_interfaces(
        original_user_goal="g", frozen_function_items=items,
        planner_model="p", model_call=model,
    )
    assert calls == 2
    assert [value["interface_id"] for value in result["interfaces"]] == ["I1", "I2"]


@pytest.mark.asyncio
async def test_semantic_reviewer_drives_one_repair_for_valid_but_reversed_members():
    items = [
        {**item("scripts/unit_1.py", ["request"], ["intermediate"]), "purpose": "produce an intermediate value"},
        {**item("scripts/unit_2.py", ["intermediate"], ["result"]), "purpose": "consume the intermediate value"},
    ]
    reversed_plan = plan(m2m("I1", "scripts/unit_2.py", "scripts/unit_1.py", "transfer intermediate"))
    corrected_plan = plan(m2m("I1", "scripts/unit_1.py", "scripts/unit_2.py", "transfer intermediate"))
    responses = iter([
        json.dumps(reversed_plan),
        json.dumps({"passed": False, "issues": [{
            "code": "interface_semantic_inconsistency", "category": "semantic_alignment_error",
            "interface_id": "I1", "message": "Direction conflicts with the declared responsibilities.",
            "evidence": {"source_purpose": "consume", "target_purpose": "produce"},
        }]}),
        json.dumps(corrected_plan),
    ])
    calls = 0

    async def model(_messages, _model):
        nonlocal calls
        calls += 1
        return next(responses)

    result = await plan_function_item_interfaces(
        original_user_goal="produce then consume", frozen_function_items=items,
        planner_model="p", reviewer_model="r", model_call=model,
    )
    assert calls == 3
    assert result == corrected_plan


def test_issue_merge_is_stable_deterministic_first_and_deduplicated():
    deterministic = [{"code": "c1", "category": "reference_error", "interface_id": "I1", "path": "$.a"}]
    duplicate = dict(deterministic[0])
    review = [{"code": "c2", "category": "semantic_alignment_error", "interface_id": "I2", "path": "$.b", "details": {"evidence": {"purpose": "x"}}}]
    assert merge_interface_validation_issues(deterministic, [duplicate], review) == [deterministic[0], review[0]]
    scope = build_interface_repair_scope([deterministic[0], review[0]], plan(
        m2m("I1", "scripts/unit_1.py", "scripts/unit_2.py"),
        m2m("I2", "scripts/unit_2.py", "scripts/unit_3.py"),
        m2m("I3", "scripts/unit_3.py", "scripts/unit_4.py"),
    ))
    assert scope["affected_interface_ids"] == ["I1", "I2"]


@pytest.mark.asyncio
async def test_deterministic_and_review_issues_share_context_and_one_repair():
    allocations = [allocation("R-system", [])]
    items = [
        item("scripts/unit_1.py", ["a"], ["b"]),
        item("scripts/unit_2.py", ["b"], ["c"]),
    ]
    initial = plan(
        m2m("I1", "missing", "scripts/unit_2.py"),
        m2m("I2", "scripts/unit_2.py", "scripts/unit_1.py"),
    )
    repaired = plan(
        m2m("I1", "scripts/unit_1.py", "scripts/unit_2.py"),
        m2m("I2", "scripts/unit_1.py", "scripts/unit_2.py"),
    )
    responses = iter([
        json.dumps(initial),
        json.dumps({"passed": False, "issues": [{
            "code": "interface_semantic_inconsistency", "category": "semantic_alignment_error",
            "interface_id": "I2", "message": "Direction conflicts with responsibilities.",
            "evidence": {"source_purpose": "consumer", "target_purpose": "producer"},
        }]}),
        json.dumps(repaired),
    ])
    payloads = []

    async def model(messages, _model):
        payloads.append(json.loads(messages[-1]["content"]))
        return next(responses)

    result = await plan_function_item_interfaces(
        original_user_goal="g", frozen_function_items=items,
        requirement_allocations=allocations, requirement_channels={"R-system": "direct"},
        planner_model="p", reviewer_model="r", model_call=model,
    )
    assert result == repaired
    assert len(payloads) == 3
    assert all(payload["unowned_system_requirements"] == allocations for payload in payloads)
    assert [issue["interface_id"] for issue in payloads[2]["validation_issues"]] == ["I1", "I2"]


@pytest.mark.asyncio
async def test_explicit_empty_system_requirements_never_fall_back():
    captured = []

    async def model(messages, _model):
        captured.append(json.loads(messages[-1]["content"]))
        return json.dumps(plan(p2m("I1", "scripts/unit_1.py")))

    await plan_function_item_interfaces(
        original_user_goal="g", frozen_function_items=[item("scripts/unit_1.py", ["a"], ["b"])],
        requirement_allocations=[allocation("R-system", [])], interaction_requirements=[],
        planner_model="p", model_call=model,
    )
    assert captured[0]["unowned_system_requirements"] == []


@pytest.mark.asyncio
async def test_reviewer_protocol_repair_runs_once_then_semantic_repair():
    items = [item("scripts/unit_1.py", ["a"], ["b"])]
    initial = plan(p2m("I1", "scripts/unit_1.py"))
    responses = iter([
        json.dumps(initial),
        json.dumps({"review": {"passed": False}}),
        json.dumps({"passed": False, "issues": [{
            "code": "interface_semantic_inconsistency", "category": "semantic_alignment_error",
            "interface_id": "I1", "message": "Boundary conflicts with the goal.",
            "evidence": {"platform_contract": "input"},
        }]}),
        json.dumps(m2p_plan := plan(m2p("I1", "scripts/unit_1.py"))),
    ])
    calls = 0

    async def model(_messages, _model):
        nonlocal calls
        calls += 1
        return next(responses)

    assert await plan_function_item_interfaces(
        original_user_goal="g", frozen_function_items=items,
        planner_model="p", reviewer_model="r", model_call=model,
    ) == m2p_plan
    assert calls == 4


@pytest.mark.asyncio
async def test_reviewer_protocol_repair_failure_blocks_without_semantic_repair():
    responses = iter(["bad", "still bad"])
    calls = 0

    async def model(_messages, _model):
        nonlocal calls
        calls += 1
        return next(responses)

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await review_interface_plan_semantically(
            original_user_goal="g", frozen_function_items=[item("scripts/unit_1.py", [], [])],
            interface_plan=plan(m2p("I1", "scripts/unit_1.py")),
            requirement_allocations=[], requirement_channels={}, system_requirements=[],
            platform_contract={}, reviewer_model="r", model_call=model,
        )
    assert calls == 2
    assert raised.value.code == "interface_semantic_review_failed"
    assert raised.value.details["protocol_repair_attempts"] == 1
