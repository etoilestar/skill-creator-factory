"""Minimal deterministic closure and edge-patch contract tests."""

from backend.services.creator.function_item_interface_plan import (
    collect_interface_plan_validation_issues,
    validate_interface_patch,
)


def items():
    return [{
        "target_file": "scripts/run.py", "role": "worker", "purpose": "run",
        "inputs": [
            {"port_id": "left", "role": "required_runtime_input", "contract": {"type": "string"}},
            {"port_id": "right", "role": "required_runtime_input", "contract": {"type": "string"}},
        ],
        "outputs": [{"port_id": "report", "role": "runtime_output", "contract": {"type": "string"}}],
        "constraints": [], "required_capabilities": [],
    }]


def platform():
    return {"platform_skill_boundary": {
        "input_envelope_fields": ["left", "right"],
        "input_schemas": {"left": {"type": "string"}, "right": {"type": "string"}},
        "final_output_fields": ["result"], "required_final_output_fields": ["result"],
        "output_sinks": {"result": {"semantic_type": "text", "accepted_source_types": ["text"]}},
    }}


def binding(iid, source, target):
    return {"interface_id": iid, "kind": "platform_to_member", "source_platform_input": source,
            "source_path": [], "target_member": "scripts/run.py", "target_input": target, "goal": "bind"}


def complete_plan(second_source="right"):
    return {"interfaces": [
        binding("I1", "left", "left"), binding("I2", second_source, "right"),
        {"interface_id": "I3", "kind": "member_to_platform", "source_member": "scripts/run.py",
         "source_output": "report", "target_platform_output": "result", "goal": "return"},
    ]}


def test_two_platform_inputs_close_two_member_slots():
    assert collect_interface_plan_validation_issues(
        plan=complete_plan(), function_items=items(), platform_contract=platform()) == []


def test_invalid_source_is_addressed_to_interface_id():
    issues = collect_interface_plan_validation_issues(
        plan=complete_plan("missing"), function_items=items(), platform_contract=platform())
    diagnostic = next(issue for issue in issues if issue["code"] == "interface_binding_invalid")
    assert diagnostic["interface_id"] == "I2"
    assert diagnostic["source"]["platform_input"] == "missing"


def test_replace_source_patch_restores_closure():
    broken = complete_plan("missing")
    violations = collect_interface_plan_validation_issues(
        plan=broken, function_items=items(), platform_contract=platform())
    repaired = validate_interface_patch(
        plan=broken, function_items=items(), platform_contract=platform(), violations=violations,
        patch={"operations": [{"op": "replace_source", "interface_id": "I2", "reason": "use frozen port",
                               "source_platform_input": "right", "source_path": []}]},
    )
    assert collect_interface_plan_validation_issues(
        plan=repaired, function_items=items(), platform_contract=platform()) == []


def test_patch_preserves_valid_interface_exactly():
    broken = complete_plan("missing")
    frozen = dict(broken["interfaces"][0])
    violations = collect_interface_plan_validation_issues(
        plan=broken, function_items=items(), platform_contract=platform())
    repaired = validate_interface_patch(
        plan=broken, function_items=items(), platform_contract=platform(), violations=violations,
        patch={"operations": [{"op": "replace_source", "interface_id": "I2", "reason": "use frozen port",
                               "source_platform_input": "right", "source_path": []}]},
    )
    assert repaired["interfaces"][0] == frozen
