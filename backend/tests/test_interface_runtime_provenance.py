import pytest

from backend.services.creator.function_item_interface_plan import (
    apply_interface_patch,
    build_canonical_interface_contract,
    build_runtime_binding_facts,
    collect_interface_plan_validation_issues,
)


def _items():
    return [{
        "target_file": "scripts/compare_csv.py", "role": "worker", "purpose": "compare",
        "inputs": [
            {"port_id": "input_files", "role": "required_runtime_input", "contract": {"type": "array", "semantic_type": "csv_files"}},
            {"port_id": "primary_key_field", "role": "required_runtime_input", "contract": {"type": "string", "semantic_type": "primary_key"}},
        ],
        "outputs": [{"port_id": "result", "role": "runtime_output", "contract": {"type": "string"}}],
        "constraints": [], "required_capabilities": [],
    }]


def _platform():
    return {"platform_skill_boundary": {
        "input_envelope_fields": ["input_files", "options"],
        "input_schemas": {
            "input_files": {"type": "array", "semantic_type": "csv_files"},
            "options": {"type": "object", "properties": {
                "primary_key_field": {"type": "string", "semantic_type": "primary_key"},
            }},
        },
        "final_output_fields": ["text"],
    }}


def _binding(iid, source, target, path=None):
    return {"interface_id": iid, "kind": "platform_to_member", "source_platform_input": source,
            "source_path": path or [], "target_member": "scripts/compare_csv.py",
            "target_input": target, "goal": "frozen runtime source"}


def test_two_sources_for_one_runtime_slot_are_reported():
    plan = {"interfaces": [_binding("I1", "input_files", "primary_key_field"),
                           _binding("I2", "options", "primary_key_field", ["primary_key_field"])]}
    issues = collect_interface_plan_validation_issues(plan=plan, function_items=_items(), platform_contract=_platform())
    duplicate = next(issue for issue in issues if issue["code"] == "multiple_runtime_sources")
    assert duplicate["target"] == "scripts/compare_csv.py.primary_key_field"
    assert duplicate["sources"] == ["input_files", "options.primary_key_field"]


def test_nested_declared_runtime_source_passes_provenance():
    facts = build_runtime_binding_facts(function_items=_items(), platform_contract=_platform())
    # Candidate selection is owned by the semantic planner, not projected from
    # coincidentally matching field names.
    assert facts["scripts/compare_csv.py"]["primary_key_field"]["allowed_sources"] == []
    issues = collect_interface_plan_validation_issues(
        plan={"interfaces": [_binding("I1", "input_files", "input_files"),
                             _binding("I2", "options", "primary_key_field", ["primary_key_field"])]},
        function_items=_items(), platform_contract=_platform())
    assert not [issue for issue in issues if issue["code"] in {"incompatible_semantic_provenance", "uncovered_required_logical_input", "multiple_runtime_sources"}]


def test_name_like_wrong_source_is_rejected_and_not_covered():
    issues = collect_interface_plan_validation_issues(
        plan={"interfaces": [_binding("I1", "input_files", "primary_key_field")]},
        function_items=_items(), platform_contract=_platform())
    codes = {issue["code"] for issue in issues}
    assert {"incompatible_semantic_provenance", "uncovered_required_logical_input"} <= codes


def test_patch_removes_invalid_interface_and_converges():
    current = {"interfaces": [_binding("I1", "input_files", "input_files"),
                              _binding("I2", "input_files", "primary_key_field"),
                              _binding("I3", "options", "primary_key_field", ["primary_key_field"])]}
    repaired = apply_interface_patch(current, {"operations": [{
        "op": "remove_interface", "interface_id": "I2", "reason": "invalid runtime provenance",
    }]})
    issues = collect_interface_plan_validation_issues(plan=repaired, function_items=_items(), platform_contract=_platform())
    assert not [issue for issue in issues if issue["code"] in {"multiple_runtime_sources", "incompatible_semantic_provenance", "uncovered_required_logical_input"}]
    assert build_canonical_interface_contract(plan=repaired, function_items=_items(), platform_contract=_platform())["interfaces"][1]["runtime_provenance"] == {
        "source_type": "platform_input", "source_platform_input": "options", "source_path": ["primary_key_field"]}
