import json

import pytest

from backend.services.platform_io_contract import build_platform_io_contract
from backend.services.creator.responsibility_graph_expansion import (
    GraphExpansionState,
    ResponsibilityGraphExpansionError,
    build_binding_candidates_for_obligation,
    build_terminal_binding_candidates,
    enqueue_required_inputs_for_node,
    expand_responsibility_graph,
    initialize_state_from_terminals,
    validate_binding_selection_protocol,
    validate_terminal_selection_protocol,
)


def item(node, inputs, outputs, defaults=None):
    return {
        "target_file": node,
        "role": "script",
        "purpose": f"purpose-{node}",
        "inputs": inputs,
        "outputs": outputs,
        "required_capabilities": [],
        "constraints": [],
        "default_values": defaults or {},
    }


def contract(outputs=None):
    boundary = {
        "input_envelope_fields": ["input", "payload"],
        "final_output_fields": outputs or ["text", "pdf_path", "image_path", "docx_path"],
    }
    return {"platform_skill_boundary": boundary}


def scripted_model(terminal_node, terminal_output, terminal_field, bindings, calls=None, terminal_fields=None):
    """Choose by supplied semantic contexts, never by backend edge data."""
    remaining = list(bindings)

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if calls is not None:
            calls.append(payload)
        if "terminal_candidates" in payload:
            wanted = terminal_fields or [terminal_field]
            ids = []
            for field in wanted:
                candidate = next(value for value in payload["terminal_candidates"] if
                    value["source_context"]["node_purpose"] == f"purpose-{terminal_node}"
                    and value["source_context"]["output_name"] == terminal_output
                    and value["target_context"]["platform_output_field"] == field)
                ids.append(candidate["binding_id"])
            return json.dumps({"terminal_binding_ids": ids})
        source_node, source_port = remaining.pop(0)
        if source_node == "platform_input_node":
            candidate = next(value for value in payload["binding_candidates"] if value["source_context"].get("platform_input_field") == source_port)
        else:
            candidate = next(value for value in payload["binding_candidates"] if
                value["source_context"].get("node_purpose") == f"purpose-{source_node}"
                and value["source_context"].get("output_name") == source_port)
        return json.dumps({"selection": {"obligation_id": payload["obligation"]["obligation_id"], "candidate_id": candidate["candidate_id"]}})

    return model


def test_real_platform_contract_exposes_all_terminal_fields_without_text_default():
    items = [item("scripts/a.py", [], ["q1"])]
    candidates = build_terminal_binding_candidates(
        function_items=items, platform_contract=build_platform_io_contract()
    )
    fields = {value["target"]["port_id"] for value in candidates}
    assert {"text", "pdf_path", "image_path", "docx_path"} <= fields
    for field in ("pdf_path", "image_path", "docx_path"):
        assert any(value["target"]["port_id"] == field for value in candidates)


@pytest.mark.asyncio
async def test_goal_driven_serial_activation_order_and_determinism():
    items = [
        item("scripts/a.py", ["a_in"], ["a_out"]),
        item("scripts/b.py", ["b_in"], ["b_out"]),
        item("scripts/c.py", ["c_in"], ["c_out"]),
    ]
    calls = []
    choices = [("scripts/b.py", "b_out"), ("scripts/a.py", "a_out"), ("platform_input_node", "input")]
    model = scripted_model("scripts/c.py", "c_out", "pdf_path", choices, calls)
    first = await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={"user_request": "g"})
    second = await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=scripted_model("scripts/c.py", "c_out", "pdf_path", choices), goal_context={})
    assert first == second
    assert [(edge["from_node"], edge["to_node"]) for edge in first] == [
        ("scripts/c.py", "platform_output_node"),
        ("scripts/b.py", "scripts/c.py"),
        ("scripts/a.py", "scripts/b.py"),
        ("platform_input_node", "scripts/a.py"),
    ]
    input_calls = [value for value in calls if "obligation" in value]
    assert [value["obligation"]["target"]["node_id"] for value in input_calls] == ["scripts/c.py", "scripts/b.py", "scripts/a.py"]


@pytest.mark.asyncio
async def test_branch_merge_activates_only_goal_ancestors(caplog):
    caplog.set_level("INFO")
    items = [
        item("scripts/a.py", ["a_in"], ["a_out"]),
        item("scripts/b.py", ["b_in"], ["b_out"]),
        item("scripts/c.py", ["left", "right"], ["c_out"]),
        item("scripts/d.py", ["unused"], ["d_out"]),
    ]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(
            function_items=items, platform_contract=contract(), planner_model="p",
            model_call=scripted_model("scripts/c.py", "c_out", "image_path", [
                ("scripts/a.py", "a_out"), ("scripts/b.py", "b_out"),
                ("platform_input_node", "input"), ("platform_input_node", "payload"),
            ]), goal_context={},
        )
    assert raised.value.code == "inactive_frozen_function_items"
    assert raised.value.details["targets"] == ["scripts/d.py"]
    assert "scripts/d.py" in caplog.text


def test_frontier_skips_defaults_duplicates_and_inactive_nodes():
    items = [item("scripts/a.py", ["x", "y"], ["z"], {"y": 4})]
    inactive = GraphExpansionState()
    enqueue_required_inputs_for_node(node_id="scripts/a.py", function_items=items, state=inactive)
    assert not inactive.frontier
    state = GraphExpansionState(active_nodes={"scripts/a.py"})
    enqueue_required_inputs_for_node(node_id="scripts/a.py", function_items=items, state=state)
    enqueue_required_inputs_for_node(node_id="scripts/a.py", function_items=items, state=state)
    assert [(value["target"]["port_id"]) for value in state.frontier] == ["x"]


def test_terminal_protocol_rejects_unknown_duplicate_slots_and_edge_fields():
    items = [item("scripts/a.py", [], ["z"]), item("scripts/b.py", [], ["q"])]
    candidates = build_terminal_binding_candidates(function_items=items, platform_contract=contract(["text"]))
    cases = [
        {"terminal_binding_ids": []},
        {"terminal_binding_ids": ["outside"]},
        {"terminal_binding_ids": [candidates[0]["binding_id"], candidates[0]["binding_id"]]},
        {"terminal_binding_ids": [candidates[0]["binding_id"], candidates[1]["binding_id"]]},
        {"terminal_binding_ids": [candidates[0]["binding_id"]], "edge": {}},
    ]
    for response in cases:
        with pytest.raises(ResponsibilityGraphExpansionError):
            validate_terminal_selection_protocol(candidates=candidates, response=response)


def test_terminal_protocol_requires_every_explicit_required_output():
    items = [item("scripts/a.py", [], ["z"])]
    required_contract = contract(["pdf_path", "docx_path"])
    required_contract["platform_skill_boundary"]["required_final_output_fields"] = [
        "pdf_path", "docx_path"
    ]
    candidates = build_terminal_binding_candidates(
        function_items=items, platform_contract=required_contract
    )
    pdf = next(
        value for value in candidates if value["target"]["port_id"] == "pdf_path"
    )
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        validate_terminal_selection_protocol(
            candidates=candidates,
            response={"terminal_binding_ids": [pdf["binding_id"]]},
        )
    assert raised.value.code == "missing_required_terminal_binding"
    assert raised.value.details["missing_required_final_output_fields"] == [
        "docx_path"
    ]


def test_binding_protocol_rejects_unknown_candidate_and_edge_fields():
    items = [item("scripts/a.py", ["x"], ["z"])]
    terminals = build_terminal_binding_candidates(function_items=items, platform_contract=contract(["text"]))
    state = initialize_state_from_terminals(terminal_ids=[terminals[0]["binding_id"]], candidates=terminals, function_items=items)
    obligation = state.frontier[0]
    candidates = build_binding_candidates_for_obligation(obligation=obligation, function_items=items, platform_contract=contract(), state=state)
    for response in (
        {"selection": {"obligation_id": obligation["obligation_id"], "candidate_id": "outside"}},
        {"selection": {"obligation_id": obligation["obligation_id"], "candidate_id": candidates[0]["candidate_id"], "from_node": "x"}},
    ):
        with pytest.raises(ResponsibilityGraphExpansionError):
            validate_binding_selection_protocol(obligation=obligation, candidates=candidates, response=response)


@pytest.mark.asyncio
async def test_multiple_terminals_are_allowed_on_distinct_slots():
    items = [item("scripts/a.py", [], ["z"])]
    edges = await expand_responsibility_graph(
        function_items=items, platform_contract=contract(), planner_model="p",
        model_call=scripted_model("scripts/a.py", "z", "text", [], terminal_fields=["pdf_path", "docx_path"]), goal_context={},
    )
    assert [edge["to_input"] for edge in edges] == ["pdf_path", "docx_path"]


def test_cycle_source_is_not_a_candidate_and_unknown_types_remain():
    items = [item("scripts/a.py", ["x"], ["a"]), item("scripts/b.py", ["y"], ["b"])]
    state = GraphExpansionState(
        active_nodes={"scripts/a.py", "scripts/b.py"},
        committed_edges=[{"from_node": "scripts/a.py", "from_output": "a", "to_node": "scripts/b.py", "to_input": "y", "purpose": "p", "constraints": []}],
    )
    obligation = {"obligation_id": "O0001", "target": {"node_id": "scripts/a.py", "port_id": "x"}, "target_context": {"input_contract": {"type": "unknown"}}}
    candidates = build_binding_candidates_for_obligation(obligation=obligation, function_items=items, platform_contract=contract(), state=state)
    assert "scripts/b.py" not in {value["edge"]["from_node"] for value in candidates}
    assert "platform_input_node" in {value["edge"]["from_node"] for value in candidates}


@pytest.mark.asyncio
async def test_random_rename_preserves_topology():
    async def topology(node, input_name, output_name):
        edges = await expand_responsibility_graph(
            function_items=[item(node, [input_name], [output_name])], platform_contract=contract(), planner_model="p",
            model_call=scripted_model(node, output_name, "docx_path", [("platform_input_node", "payload")]), goal_context={},
        )
        return [(edge["from_node"] == "platform_input_node", edge["to_node"] == "platform_output_node") for edge in edges]
    assert await topology("scripts/q7.py", "q8", "q9") == await topology("scripts/v2.py", "v3", "v4")


@pytest.mark.asyncio
async def test_invalid_candidate_retries_only_current_obligation():
    items = [item("scripts/a.py", ["x"], ["z"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if "terminal_candidates" in payload:
            return json.dumps({"terminal_binding_ids": [payload["terminal_candidates"][0]["binding_id"]]})
        if len([value for value in calls if "obligation" in value]) == 1:
            return json.dumps({"selection": {"obligation_id": payload["obligation"]["obligation_id"], "candidate_id": "outside"}})
        return json.dumps({"selection": {"obligation_id": payload["obligation"]["obligation_id"], "candidate_id": payload["binding_candidates"][0]["candidate_id"]}})

    edges = await expand_responsibility_graph(
        function_items=items, platform_contract=contract(["text"]),
        planner_model="p", model_call=model, goal_context={},
    )
    input_calls = [value for value in calls if "obligation" in value]
    assert len(input_calls) == 2
    assert input_calls[1]["validation_issue"]["affected_obligation_id"] == "O0001"
    assert len(edges) == 2


@pytest.mark.asyncio
async def test_invalid_terminal_selection_retries_once_with_same_domain():
    items = [item("scripts/a.py", [], ["z"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            return json.dumps({"terminal_binding_ids": ["outside"]})
        return json.dumps({
            "terminal_binding_ids": [payload["terminal_candidates"][0]["binding_id"]]
        })

    edges = await expand_responsibility_graph(
        function_items=items,
        platform_contract=contract(["text"]),
        planner_model="p",
        model_call=model,
        goal_context={},
    )
    assert len(calls) == 2
    assert calls[0]["terminal_candidates"] == calls[1]["terminal_candidates"]
    assert calls[1]["protocol_error"]["code"] == "invalid_terminal_selection_protocol"
    assert len(edges) == 1
