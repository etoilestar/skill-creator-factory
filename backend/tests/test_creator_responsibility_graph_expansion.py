import json

import pytest

from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError,
    build_endpoint_registry,
    expand_responsibility_graph,
    _validate_interface_selection_protocol,
)


def test_endpoint_protocol_rejects_every_extra_field():
    from backend.services.creator.responsibility_graph_expansion import _validate_interface_selection_protocol

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        _validate_interface_selection_protocol(
            obligation={"kind": "platform_to_script"},
            response={"source_id": "S", "target_id": "T", "source_path": []},
        )
    assert raised.value.code == "invalid_interface_endpoint_protocol"

from backend.services.platform_io_contract import build_platform_io_contract


@pytest.mark.asyncio
async def test_empty_endpoint_candidate_domain_fails_without_model_call():
    calls = 0

    async def model(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("empty endpoint domains must not call the model")

    function_items = [{
        "target_file": "scripts/a.py", "role": "script", "purpose": "produce output",
        "inputs": [], "outputs": [], "required_capabilities": [], "constraints": [],
        "default_values": {},
    }]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(
            function_items=function_items,
            platform_contract={"platform_skill_boundary": {"input_envelope_fields": ["fields"], "final_output_fields": ["text"]}},
            planner_model="p", model_call=model, interface_plan={"interfaces": [{
                "interface_id": "i1", "kind": "member_to_platform",
                "goal": "output_x to platform text", "source_member": "scripts/a.py",
            }]},
        )
    assert raised.value.code == "empty_interface_endpoint_domain"
    assert calls == 0


def item(node, inputs, outputs, defaults=None):
    return {"target_file": node, "role": "script", "purpose": f"purpose-{node}", "inputs": inputs, "outputs": outputs, "required_capabilities": [], "constraints": [], "default_values": defaults or {}}


def contract(required=None):
    boundary = {"input_envelope_fields": ["fields", "options"], "final_output_fields": ["text", "pdf_path", "image_path", "docx_path"]}
    if required is not None:
        boundary["required_final_output_fields"] = required
    return {"platform_skill_boundary": boundary}


def plan(*interfaces):
    return {"interfaces": list(interfaces)}


def p2m(interface_id, target):
    return {"interface_id": interface_id, "kind": "platform_to_member", "goal": "runtime input", "target_member": target}


def m2m(interface_id, source, target):
    return {"interface_id": interface_id, "kind": "member_to_member", "goal": "member result", "source_member": source, "target_member": target}


def m2p(interface_id, source):
    return {"interface_id": interface_id, "kind": "member_to_platform", "goal": "platform output", "source_member": source}


def test_platform_selection_rejects_string_source_path():
    obligation = {
        "kind": "platform_to_script",
    }

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        _validate_interface_selection_protocol(
            obligation=obligation,
            response={
                "source_id": "PIN0001",
                "target_id": "IN0001",
                "source_path": "scripts/a.py",
            },
        )

    assert raised.value.code == "invalid_interface_endpoint_protocol"
    assert raised.value.details["path"] == "$.source_path"
    assert raised.value.details["expected_type"] == "array<string>"


def test_platform_selection_accepts_empty_source_path():
    result = _validate_interface_selection_protocol(
        obligation={"kind": "platform_to_script"},
        response={
            "source_id": "PIN0001",
            "target_id": "IN0001",
            "source_path": [],
        },
    )

    assert result["source_path"] == []


def test_platform_selection_accepts_nested_source_path():
    result = _validate_interface_selection_protocol(
        obligation={"kind": "platform_to_script"},
        response={
            "source_id": "PIN0001",
            "target_id": "IN0001",
            "source_path": ["article", "text"],
        },
    )

    assert result["source_path"] == ["article", "text"]


def test_registry_has_independent_stable_endpoints_without_cartesian_product():
    items = [item(f"scripts/n{i}.py", [], [f"o{i}"]) for i in range(10)]
    platform = {"platform_skill_boundary": {"input_envelope_fields": [f"i{i}" for i in range(3)], "final_output_fields": [f"p{i}" for i in range(10)]}}
    first = build_endpoint_registry(function_items=items, platform_contract=platform)
    second = build_endpoint_registry(function_items=items, platform_contract=platform)
    assert first == second
    assert len(first["nodes"]) == len(first["script_outputs"]) == len(first["platform_outputs"]) == 10
    assert first["nodes"][0]["node_id"] == "N0001"
    assert first["script_outputs"][-1]["output_id"] == "OUT0010"
    assert first["platform_outputs"][-1]["slot_id"] == "POUT0010"
    assert all("edge" not in value for group in first.values() for value in group)


def test_one_hundred_outputs_register_no_edge_candidates():
    items = [item(f"scripts/n{i}.py", [], [f"o{i}"]) for i in range(100)]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    assert len(registry["script_outputs"]) == 100
    assert not any("candidate_id" in value or "edge" in value for value in registry["script_outputs"])


@pytest.mark.asyncio
async def test_two_script_interface_expansion_uses_endpoint_references_without_global_candidates():
    items = [item("scripts/a.py", ["a_in"], ["a_out"]), item("scripts/b.py", ["b_in"], ["b_out"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            slot = next(value for value in payload["platform_inputs"] if value["field"] == "fields")
            return json.dumps({"source_id": slot["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": ["future_91ab"]})
        if obligation["kind"] == "script_to_platform":
            slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={"system_goal": "random"}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/b.py")))
    assert [(edge["from_node"], edge["to_node"]) for edge in edges] == [("platform_input_node", "scripts/a.py"), ("scripts/a.py", "scripts/b.py"), ("scripts/b.py", "platform_output_node")]
    member_call = next(call for call in calls if call["obligation"]["kind"] == "script_to_script")
    assert "script_outputs" not in member_call
    assert "binding_candidates" not in member_call


@pytest.mark.asyncio
async def test_interface_plan_missing_is_not_legacy_fallback():
    with pytest.raises(TypeError):
        await expand_responsibility_graph(function_items=[item("scripts/a.py", [], ["z"])], platform_contract=contract(), planner_model="p", model_call=lambda *_: "{}", goal_context={})


@pytest.mark.asyncio
async def test_endpoint_selection_retries_only_current_interface():
    items = [item("scripts/a.py", ["x"], ["z"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if payload["obligation"]["kind"] == "platform_to_script":
            if len(calls) == 1:
                return json.dumps({"source_id": "PIN9999", "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert calls[1]["validation_error"]["code"] == "invalid_interface_endpoint_reference"


@pytest.mark.asyncio
async def test_structured_port_defaults_and_unresolved_inputs_use_port_ids():
    items = [item("scripts/a.py", [{"port_id": "provided", "description": ""}, {"port_id": "defaulted", "description": ""}], ["z"], defaults={"defaulted": "ok"})]

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if payload["obligation"]["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert ("scripts/a.py", "provided") in {(edge["to_node"], edge["to_input"]) for edge in edges}

    missing_items = [item("scripts/a.py", [{"port_id": "first"}, {"port_id": "second"}], ["z"])]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=missing_items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert raised.value.code == "interface_plan_incomplete"
    assert raised.value.details["uncovered_inputs"] == [{"target": "scripts/a.py", "input_id": "second"}]


def test_structured_port_description_and_contract_are_preserved_in_registry():
    items = [
        item(
            "scripts/a.py",
            [{"port_id": "input_1", "description": "input description", "contract": {"type": "object"}}],
            [{"port_id": "output_1", "description": "output description", "contract": {"type": "string"}}],
        )
    ]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    assert registry["script_inputs"][0]["description"] == "input description"
    assert registry["script_inputs"][0]["contract"] == {"type": "object"}
    assert registry["script_outputs"][0]["description"] == "output description"
    assert registry["script_outputs"][0]["contract"] == {"type": "string"}


@pytest.mark.asyncio
async def test_endpoint_payload_keeps_structured_port_metadata_and_type_conflicts_use_contract():
    items = [
        item(
            "scripts/a.py",
            [{"port_id": "input_1", "description": "input description", "contract": {"type": "object"}}],
            [{"port_id": "output_1", "description": "output description", "contract": {"type": "object"}}],
        ),
        item(
            "scripts/b.py",
            [{"port_id": "input_1", "description": "target description", "contract": {"type": "string"}}],
            ["output_1"],
        ),
    ]
    captured = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        captured.append(payload)
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
        if obligation["kind"] == "script_to_script":
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/b.py")))
    assert raised.value.code == "interface_endpoint_type_conflict"
    member_call = next(call for call in captured if call["obligation"]["kind"] == "script_to_script")
    assert member_call["source_member_outputs"][0]["description"] == "output description"
    assert member_call["source_member_outputs"][0]["contract"] == {"type": "object"}
    assert member_call["target_member_inputs"][0]["description"] == "target description"
    assert member_call["target_member_inputs"][0]["contract"] == {"type": "string"}


@pytest.mark.asyncio
async def test_missing_platform_output_reports_required_fields_without_terminal_edges():
    items = [item("scripts/a.py", [], ["output_1"])]
    platform = {"platform_skill_boundary": {"input_envelope_fields": ["fields"], "final_output_fields": ["text", "pdf_path"], "required_final_output_fields": ["text", "pdf_path"]}}

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=platform, planner_model="p", model_call=lambda *_: "{}", goal_context={}, interface_plan=plan())
    assert raised.value.code == "interface_plan_incomplete"
    assert raised.value.details["missing_required_final_output_fields"] == ["pdf_path", "text"]



@pytest.mark.asyncio
async def test_repeated_member_interfaces_bind_distinct_unbound_inputs():
    items = [
        item("scripts/a.py", ["runtime_input"], ["first", "second", "third"]),
        item("scripts/b.py", ["first", "second", "third"], ["result"]),
    ]

    interface_plan = plan(
        p2m("I0001", "scripts/a.py"),
        m2m("I0002", "scripts/a.py", "scripts/b.py"),
        m2m("I0003", "scripts/a.py", "scripts/b.py"),
        m2m("I0004", "scripts/a.py", "scripts/b.py"),
        m2p("I0005", "scripts/b.py"),
    )
    seen_target_lists = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({
                "source_id": payload["platform_inputs"][0]["slot_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
                "source_path": [],
            })
        if obligation["kind"] == "script_to_script":
            seen_target_lists.append([value["port_id"] for value in payload["target_member_inputs"]])
            return json.dumps({
                "source_id": payload["source_member_outputs"][len(seen_target_lists) - 1]["output_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
            })
        return json.dumps({
            "source_id": payload["source_member_outputs"][0]["output_id"],
            "target_id": payload["platform_outputs"][0]["slot_id"],
        })

    edges = await expand_responsibility_graph(
        function_items=items,
        platform_contract=contract(required=["text"]),
        planner_model="p",
        goal_context={},
        model_call=model,
        interface_plan=interface_plan,
    )

    assert seen_target_lists == [["first", "second", "third"], ["second", "third"], ["third"]]
    assert {(edge["to_node"], edge["to_input"]) for edge in edges} >= {
        ("scripts/b.py", "first"),
        ("scripts/b.py", "second"),
        ("scripts/b.py", "third"),
    }


@pytest.mark.asyncio
async def test_source_output_may_fan_out_to_multiple_targets():
    items = [
        item("scripts/source.py", ["runtime_input"], ["shared_result"]),
        item("scripts/consumer_a.py", ["input_a"], ["result_a"]),
        item("scripts/consumer_b.py", ["input_b"], ["result_b"]),
    ]
    interface_plan = plan(
        p2m("I0001", "scripts/source.py"),
        m2m("I0002", "scripts/source.py", "scripts/consumer_a.py"),
        m2m("I0003", "scripts/source.py", "scripts/consumer_b.py"),
        m2p("I0004", "scripts/consumer_a.py"),
        m2p("I0005", "scripts/consumer_b.py"),
    )
    selected_source_ids = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({
                "source_id": payload["platform_inputs"][0]["slot_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
                "source_path": [],
            })
        if obligation["kind"] == "script_to_script":
            source_id = payload["source_member_outputs"][0]["output_id"]
            selected_source_ids.append(source_id)
            return json.dumps({
                "source_id": source_id,
                "target_id": payload["target_member_inputs"][0]["input_id"],
            })
        return json.dumps({
            "source_id": payload["source_member_outputs"][0]["output_id"],
            "target_id": payload["platform_outputs"][0]["slot_id"],
        })

    await expand_responsibility_graph(
        function_items=items,
        platform_contract=contract(required=["text", "pdf_path"]),
        planner_model="p",
        goal_context={},
        model_call=model,
        interface_plan=interface_plan,
    )

    assert len(selected_source_ids) == 2
    assert selected_source_ids[0] == selected_source_ids[1]


@pytest.mark.asyncio
async def test_target_input_is_removed_after_first_binding():
    items = [
        item("scripts/source.py", ["runtime_input"], ["shared_result"]),
        item("scripts/target.py", ["first", "second"], ["result"]),
    ]
    seen_target_lists = []
    seen_source_lists = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({
                "source_id": payload["platform_inputs"][0]["slot_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
                "source_path": [],
            })
        if obligation["kind"] == "script_to_script":
            seen_target_lists.append([value["port_id"] for value in payload["target_member_inputs"]])
            seen_source_lists.append([value["port_id"] for value in payload["source_member_outputs"]])
            return json.dumps({
                "source_id": payload["source_member_outputs"][0]["output_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
            })
        return json.dumps({
            "source_id": payload["source_member_outputs"][0]["output_id"],
            "target_id": payload["platform_outputs"][0]["slot_id"],
        })

    await expand_responsibility_graph(
        function_items=items,
        platform_contract=contract(required=["text"]),
        planner_model="p",
        goal_context={},
        model_call=model,
        interface_plan=plan(
            p2m("I0001", "scripts/source.py"),
            m2m("I0002", "scripts/source.py", "scripts/target.py"),
            m2m("I0003", "scripts/source.py", "scripts/target.py"),
            m2p("I0004", "scripts/target.py"),
        ),
    )

    assert seen_target_lists == [["first", "second"], ["second"]]
    assert seen_source_lists == [["shared_result"], ["shared_result"]]


@pytest.mark.asyncio
async def test_overcomplete_only_when_no_remaining_target_endpoint():
    items = [
        item("scripts/source.py", ["runtime_input"], ["shared_result"]),
        item("scripts/target.py", ["only"], ["result"]),
    ]

    script_to_script_calls = 0

    async def model(messages, _model):
        nonlocal script_to_script_calls
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({
                "source_id": payload["platform_inputs"][0]["slot_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
                "source_path": [],
            })
        if obligation["kind"] == "script_to_script":
            script_to_script_calls += 1
            return json.dumps({
                "source_id": payload["source_member_outputs"][0]["output_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
            })
        return json.dumps({
            "source_id": payload["source_member_outputs"][0]["output_id"],
            "target_id": payload["platform_outputs"][0]["slot_id"],
        })

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(
            function_items=items,
            platform_contract=contract(required=["text"]),
            planner_model="p",
            goal_context={},
            model_call=model,
            interface_plan=plan(
                p2m("I0001", "scripts/source.py"),
                m2m("I0002", "scripts/source.py", "scripts/target.py"),
                m2m("I0003", "scripts/source.py", "scripts/target.py"),
                m2p("I0004", "scripts/target.py"),
            ),
        )

    assert raised.value.code == "interface_plan_overcomplete"
    assert raised.value.details["interface_id"] == "I0003"
    assert raised.value.details["reason"] == "no_remaining_target_endpoint"
    assert "source" not in raised.value.details["reason"]
    assert script_to_script_calls == 1

@pytest.mark.asyncio
async def test_platform_endpoint_retry_corrects_string_source_path_to_array():
    items = [
        item(
            "scripts/a.py",
            ["user_request"],
            ["result"],
        )
    ]

    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)

        obligation = payload["obligation"]

        if obligation["kind"] == "platform_to_script":
            if len(calls) == 1:
                return json.dumps({
                    "source_id": payload["platform_inputs"][0]["slot_id"],
                    "target_id": payload["target_member_inputs"][0]["input_id"],
                    "source_path": "scripts/a.py",
                })

            assert payload["validation_error"]["details"]["path"] == "$.source_path"
            assert (
                payload["validation_error"]["details"]["expected_type"]
                == "array<string>"
            )
            assert (
                payload["previous_selection"]["source_path"]
                == "scripts/a.py"
            )

            return json.dumps({
                "source_id": payload["platform_inputs"][0]["slot_id"],
                "target_id": payload["target_member_inputs"][0]["input_id"],
                "source_path": [],
            })

        text_slot = next(
            value
            for value in payload["platform_outputs"]
            if value["field"] == "text"
        )

        return json.dumps({
            "source_id": payload["source_member_outputs"][0]["output_id"],
            "target_id": text_slot["slot_id"],
        })

    edges = await expand_responsibility_graph(
        function_items=items,
        platform_contract=build_platform_io_contract(),
        planner_model="p",
        goal_context={"system_goal": "opaque goal"},
        model_call=model,
        interface_plan=plan(
            p2m("I0001", "scripts/a.py"),
            m2p("I0002", "scripts/a.py"),
        ),
    )

    assert len(calls) == 3
    assert edges[0]["from_node"] == "platform_input_node"
    assert edges[0]["to_node"] == "scripts/a.py"
    assert edges[0]["constraints"] == []
