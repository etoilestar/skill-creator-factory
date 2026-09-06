import json

import pytest

from backend.services.creator.function_item_interface_plan import (
    build_canonical_interface_contract,
    plan_function_item_interfaces,
)
from backend.services.creator.responsibility_graph_expansion import (
    expand_responsibility_graph,
)
from backend.services.skill_dataflow import resolve_context_value


def _item(element_type: str) -> dict:
    return {
        "target_file": "scripts/compare.py",
        "role": "worker",
        "purpose": "compare the two semantically selected inputs",
        "inputs": [
            {"port_id": "left_file" if element_type == "file" else "image_a",
             "role": "required_runtime_input", "contract": {"type": element_type}},
            {"port_id": "right_file" if element_type == "file" else "image_b",
             "role": "required_runtime_input", "contract": {"type": element_type}},
        ],
        "outputs": [
            {"port_id": "report", "role": "runtime_output",
             "contract": {"type": "string"}},
        ],
        "default_values": {},
        "constraints": [],
        "required_capabilities": [],
    }


def _platform(source: str, element_type: str) -> dict:
    return {"platform_skill_boundary": {
        "input_envelope_fields": [source],
        "input_schemas": {source: {"type": f"list[{element_type}]"}},
        "final_output_fields": ["final_result"],
        "required_final_output_fields": ["final_result"],
        "output_sinks": {
            "final_result": {"name": "final_result", "value_schema": {"type": "string"}},
        },
        "runtime_capability_summary": {
            "indexed_source_paths": True,
            "representation_adaptation": "runtime-owned",
        },
    }}


def _contains_forbidden_conversion_field(value) -> bool:
    if isinstance(value, dict):
        return bool({"transform", "adapter", "serializer", "conversion"} & value.keys()) or any(
            _contains_forbidden_conversion_field(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_conversion_field(child) for child in value)
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize(("element_type", "source", "targets"), [
    ("file", "input_files", ("left_file", "right_file")),
    ("image", "images", ("image_a", "image_b")),
])
async def test_semantic_list_mapping_reaches_runtime_by_index(
    element_type, source, targets,
):
    items = [_item(element_type)]
    platform = _platform(source, element_type)

    async def planner(messages, _model):
        payload = json.loads(messages[1]["content"])
        candidates = payload["semantic_mapping_candidates"]
        source_candidates = [
            candidate for candidate in candidates["input_mappings"]
            if candidate.get("source_platform_input") == source
        ]
        assert {candidate["source_contract"]["type"] for candidate in source_candidates} == {
            f"list[{element_type}]"
        }
        assert {candidate["target_contract"]["type"] for candidate in source_candidates} == {
            element_type
        }
        assert payload["runtime_capability_summary"]["indexed_source_paths"] is True
        return json.dumps({"interfaces": [
            {
                "interface_id": "I1", "kind": "platform_to_member",
                "source_platform_input": source, "source_path": ["0"],
                "target_member": "scripts/compare.py", "target_input": targets[0],
                "semantic_reason": "the first supplied item is the left-hand input",
            },
            {
                "interface_id": "I2", "kind": "platform_to_member",
                "source_platform_input": source, "source_path": ["1"],
                "target_member": "scripts/compare.py", "target_input": targets[1],
                "semantic_reason": "the second supplied item is the right-hand input",
            },
            {
                "interface_id": "I3", "kind": "member_to_platform",
                "source_member": "scripts/compare.py", "source_output": "report",
                "target_platform_output": "final_result",
                "semantic_reason": "the generated report is the final result",
            },
        ]})

    plan = await plan_function_item_interfaces(
        original_user_goal="compare two inputs", frozen_function_items=items,
        platform_contract=platform, planner_model="planner", model_call=planner,
    )
    assert not _contains_forbidden_conversion_field(plan)

    canonical = build_canonical_interface_contract(
        plan=plan, function_items=items, platform_contract=platform,
    )
    assert not _contains_forbidden_conversion_field(canonical)
    assert canonical["interfaces"][2]["source"]["field"] == "report"
    assert canonical["interfaces"][2]["target"]["field"] == "final_result"

    edges = await expand_responsibility_graph(
        function_items=items, platform_contract=platform, planner_model="unused",
        interface_plan=plan,
    )
    bindings = [edge["constraints"][0] for edge in edges if edge["from_node"] == "platform_input_node"]
    assert [binding["source_path"] for binding in bindings] == [["0"], ["1"]]
    runtime_context = {source: ["first-value", "second-value"]}
    assert [
        resolve_context_value(runtime_context, f"{source}.{binding['source_key']}")
        for binding in bindings
    ] == ["first-value", "second-value"]
