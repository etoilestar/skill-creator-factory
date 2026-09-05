import logging

import pytest

from backend.services.creator.function_item_interface_plan import (
    InterfaceIntentPlanError,
    apply_interface_patch,
    build_runtime_binding_facts,
    collect_interface_plan_validation_issues,
)


def _items():
    return [{
        "target_file": "scripts/compare_csvs.py", "role": "worker", "purpose": "compare",
        "inputs": [
            {"port_id": "input_files", "role": "required_runtime_input", "contract": {"type": "array"}},
            {"port_id": "primary_key_candidates", "role": "required_runtime_input", "contract": {"type": "array"}},
        ],
        "outputs": [{"port_id": "result", "role": "runtime_output", "contract": {"type": "string"}}],
        "constraints": [], "required_capabilities": [],
    }]


def _platform():
    return {"platform_skill_boundary": {
        "input_envelope_fields": ["input_files", "options"],
        "input_schemas": {
            "input_files": {"type": "array"},
            "options": {"type": "object", "properties": {
                "primary_key_candidates": {"type": "array"},
            }},
        },
        "final_output_fields": [],
    }}


def _binding(iid, source, target, path=None):
    return {"interface_id": iid, "kind": "platform_to_member",
            "source_platform_input": source, "source_path": path or [],
            "target_member": "scripts/compare_csvs.py", "target_input": target,
            "goal": "use frozen runtime provenance"}


def test_declared_platform_mapping_closes_required_bindings(caplog):
    caplog.set_level(logging.INFO)
    plan = {"interfaces": [
        _binding("I1", "input_files", "input_files"),
        _binding("I2", "options", "primary_key_candidates", ["primary_key_candidates"]),
    ]}
    issues = collect_interface_plan_validation_issues(
        plan=plan, function_items=_items(), platform_contract=_platform())
    assert not any(issue["code"] == "uncovered_required_logical_input" for issue in issues)
    assert "[interface_runtime_binding_facts]" in caplog.text


def test_semantic_identity_does_not_create_runtime_binding():
    items = _items()
    items[0]["inputs"][1]["contract"]["semantic_type"] = "primary_key"
    platform = _platform()
    platform["platform_skill_boundary"]["input_schemas"]["input_files"] = {
        "type": "array", "items": {"type": "object", "properties": {
            "primary_key": {"type": "string", "semantic_type": "primary_key"},
        }},
    }
    del platform["platform_skill_boundary"]["input_schemas"]["options"]["properties"]["primary_key_candidates"]
    facts = build_runtime_binding_facts(function_items=items, platform_contract=platform)
    assert facts["scripts/compare_csvs.py"]["primary_key_candidates"]["allowed_sources"] == []


def test_patch_rejects_source_outside_frozen_facts():
    plan = {"interfaces": [_binding("I1", "input_files", "input_files")]}
    facts = build_runtime_binding_facts(function_items=_items(), platform_contract=_platform())
    with pytest.raises(InterfaceIntentPlanError) as raised:
        apply_interface_patch(plan, {"operations": [{
            "op": "replace_source", "interface_id": "I1", "reason": "wrong source",
            "source_platform_input": "files", "source_path": [],
        }]}, runtime_binding_facts=facts)
    assert raised.value.code == "invalid_interface_patch_source"


def test_removing_invalid_duplicate_interface_restores_closure():
    plan = {"interfaces": [
        _binding("I1", "input_files", "input_files"),
        _binding("I2", "files", "input_files"),
        _binding("I3", "options", "primary_key_candidates", ["primary_key_candidates"]),
    ]}
    repaired = apply_interface_patch(plan, {"operations": [{
        "op": "remove_interface", "interface_id": "I2", "reason": "remove invalid duplicate",
    }]})
    issues = collect_interface_plan_validation_issues(
        plan=repaired, function_items=_items(), platform_contract=_platform())
    assert not any(issue["code"] in {
        "uncovered_required_logical_input", "multiple_runtime_sources",
    } for issue in issues)
