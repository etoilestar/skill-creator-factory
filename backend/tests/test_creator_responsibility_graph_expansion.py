import json

import pytest

from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError,
    build_legal_sources_for_obligation,
    build_responsibility_binding_obligations,
    expand_responsibility_graph,
    materialize_selected_edges,
    validate_selection_protocol,
)


def item(node, inputs, outputs):
    return {
        "target_file": node,
        "role": "script",
        "purpose": f"purpose-{node}",
        "inputs": inputs,
        "outputs": outputs,
        "required_capabilities": [],
        "constraints": [],
        "default_values": {},
    }


def contract(outputs=("text",)):
    return {"platform_skill_boundary": {
        "input_envelope_fields": ["input", "payload"],
        "final_output_fields": list(outputs),
        "required_final_output_fields": list(outputs),
    }}


def enrich(obligations, items, boundary, committed=None):
    return [
        {**obligation, "legal_sources": build_legal_sources_for_obligation(
            obligation=obligation, function_items=items,
            platform_contract=boundary, committed_edges=committed or [],
        )}
        for obligation in obligations
    ]


def select(enriched, endpoint_by_target):
    selections = []
    for obligation in enriched:
        endpoint = endpoint_by_target[(obligation["target"]["node_id"], obligation["target"]["port_id"])]
        source = next(candidate for candidate in obligation["legal_sources"] if candidate["endpoint"] == endpoint)
        selections.append({"obligation_id": obligation["obligation_id"], "source_id": source["source_id"]})
    return selections


@pytest.mark.asyncio
@pytest.mark.parametrize("items,mapping,edge_count", [
    ([item("scripts/n1.py", ["i1"], ["o1"])], {
        ("scripts/n1.py", "i1"): {"node_id": "platform_input_node", "port_id": "input"},
        ("platform_output_node", "text"): {"node_id": "scripts/n1.py", "port_id": "o1"},
    }, 2),
    ([item("scripts/n1.py", ["i1"], ["o1"]), item("scripts/n2.py", ["i2"], ["o2"])], {
        ("scripts/n1.py", "i1"): {"node_id": "platform_input_node", "port_id": "input"},
        ("scripts/n2.py", "i2"): {"node_id": "scripts/n1.py", "port_id": "o1"},
        ("platform_output_node", "text"): {"node_id": "scripts/n2.py", "port_id": "o2"},
    }, 3),
])
async def test_expands_single_and_two_node_graphs(items, mapping, edge_count):
    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        return json.dumps({"selections": select(payload["obligations"], mapping)})

    edges = await expand_responsibility_graph(
        function_items=items, platform_contract=contract(),
        planner_model="p", model_call=model,
    )
    assert len(edges) == edge_count


def test_branch_merge_multi_input_and_multiple_final_obligations():
    items = [
        item("scripts/n1.py", ["i1"], ["o1"]),
        item("scripts/n2.py", ["i2"], ["o2"]),
        item("scripts/n3.py", ["i3", "i4"], ["o3"]),
    ]
    obligations = build_responsibility_binding_obligations(
        function_items=items, platform_contract=contract(("text", "markdown"))
    )
    targets = {(entry["target"]["node_id"], entry["target"]["port_id"]) for entry in obligations}
    assert {("scripts/n3.py", "i3"), ("scripts/n3.py", "i4"), ("platform_output_node", "text"), ("platform_output_node", "markdown")} <= targets
    sources = enrich(obligations, items, contract(("text", "markdown")))
    n2 = next(entry for entry in sources if entry["target"] == {"node_id": "scripts/n2.py", "port_id": "i2"})
    assert {candidate["endpoint"]["node_id"] for candidate in n2["legal_sources"]} >= {"scripts/n1.py", "scripts/n3.py"}


def test_selection_protocol_rejects_unknown_duplicate_missing_and_edges():
    items = [item("scripts/n1.py", ["i1"], ["o1"])]
    obligations = enrich(build_responsibility_binding_obligations(
        function_items=items, platform_contract=contract()), items, contract()
    )
    first = obligations[0]
    valid = {"obligation_id": first["obligation_id"], "source_id": first["legal_sources"][0]["source_id"]}
    cases = [
        {"selections": [{**valid, "source_id": "outside"}]},
        {"selections": [valid, valid, {"obligation_id": obligations[1]["obligation_id"], "source_id": obligations[1]["legal_sources"][0]["source_id"]}]},
        {"selections": [valid]},
        {"selections": [{**valid, "from_node": "scripts/n1.py"}]},
    ]
    for response in cases:
        with pytest.raises(ResponsibilityGraphExpansionError):
            validate_selection_protocol(obligations=obligations, response=response)


def test_cycle_candidate_removed_unknown_type_kept_and_no_source_is_explicit():
    items = [item("scripts/r1.py", ["a"], ["b"]), item("scripts/r2.py", ["c"], ["d"])]
    obligations = build_responsibility_binding_obligations(function_items=items, platform_contract=contract())
    target = next(entry for entry in obligations if entry["target"]["node_id"] == "scripts/r1.py")
    committed = [{"from_node": "scripts/r1.py", "from_output": "b", "to_node": "scripts/r2.py", "to_input": "c", "purpose": "p", "constraints": []}]
    legal = build_legal_sources_for_obligation(obligation=target, function_items=items, platform_contract=contract(), committed_edges=committed)
    assert {entry["endpoint"]["node_id"] for entry in legal} >= {"platform_input_node"}
    assert "scripts/r2.py" not in {entry["endpoint"]["node_id"] for entry in legal}

    only = [item("scripts/solo.py", ["in"], [])]
    final = build_responsibility_binding_obligations(function_items=only, platform_contract=contract())[1]
    assert build_legal_sources_for_obligation(obligation=final, function_items=only, platform_contract=contract(), committed_edges=[]) == []


def test_random_rename_invariance_and_deterministic_materialization():
    def topology(nodes):
        items = [item(nodes[0], [nodes[2]], [nodes[3]])]
        obligations = enrich(build_responsibility_binding_obligations(
            function_items=items, platform_contract=contract()), items, contract())
        mapping = {
            (nodes[0], nodes[2]): {"node_id": "platform_input_node", "port_id": "input"},
            ("platform_output_node", "text"): {"node_id": nodes[0], "port_id": nodes[3]},
        }
        selections = select(obligations, mapping)
        first = materialize_selected_edges(obligations=obligations, selections=selections)
        second = materialize_selected_edges(obligations=obligations, selections=selections)
        assert first == second
        return [(edge["from_node"] == "platform_input_node", edge["to_node"] == "platform_output_node") for edge in first]

    assert topology(("scripts/q7.py", "unused", "q8", "q9")) == topology(("scripts/v2.py", "unused2", "v3", "v4"))
