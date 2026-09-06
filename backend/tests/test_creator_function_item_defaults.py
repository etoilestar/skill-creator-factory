import pytest

from backend.services.creator.function_item_interface_plan import (
    collect_interface_plan_validation_issues,
)
from backend.services.skill_plan import (
    normalize_structured_function_items,
    structured_responsibility_graph_input_provenance_gaps,
)


def _item(inputs, defaults):
    return {
        "target_file": "scripts/main.py",
        "role": "script",
        "purpose": "implement the declared operation",
        "inputs": inputs,
        "outputs": [],
        "default_values": defaults,
        "required_capabilities": [],
        "constraints": [],
    }


def test_document_business_defaults_are_declared_optional_inputs():
    item = _item(
        [
            "topic",
            {"name": "font", "required": False, "default": "宋体"},
            {"name": "page_count", "required": False, "default": 5},
        ],
        {},
    )

    normalized = normalize_structured_function_items([item], source="frozen_blueprint")

    assert normalized[0]["default_values"] == {"font": "宋体", "page_count": 5}
    assert structured_responsibility_graph_input_provenance_gaps(
        normalized, [], source="frozen_blueprint"
    ) == [("scripts/main.py", "topic")]


def test_csv_optional_default_passes_interface_source_closure():
    item = _item(
        ["input_files", {"name": "ignore_empty", "required": False, "default": True}],
        {},
    )
    item["outputs"] = ["result"]
    platform = {
        "platform_skill_boundary": {
            "input_envelope_fields": ["payload"],
            "final_output_fields": ["text"],
        }
    }
    plan = {
        "interfaces": [{
            "interface_id": "I1",
            "kind": "platform_to_member",
            "source_platform_input": "payload",
            "source_path": [],
            "target_member": "scripts/main.py",
            "target_input": "input_files",
        }, {
            "interface_id": "I2",
            "kind": "member_to_platform",
            "source_member": "scripts/main.py",
            "source_output": "result",
            "target_platform_output": "text",
        }]
    }

    assert collect_interface_plan_validation_issues(
        plan=plan, function_items=[item], platform_contract=platform
    ) == []


def test_dotted_default_resolves_to_declared_structured_input():
    normalized = normalize_structured_function_items(
        [_item(["options"], {"options.font": "宋体"})],
        source="frozen_blueprint",
    )

    assert normalized[0]["default_values"] == {"options.font": "宋体"}
    assert structured_responsibility_graph_input_provenance_gaps(
        normalized, [], source="frozen_blueprint"
    ) == []


def test_unknown_script_parameter_is_rejected_before_generation():
    with pytest.raises(ValueError, match="keys_must_resolve_to_declared_inputs"):
        normalize_structured_function_items(
            [_item(["topic"], {"unknown_parameter": "xxx"})],
            source="frozen_blueprint",
        )
