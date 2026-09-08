import pytest

from backend.services.creator import e2e
from backend.services.creator.function_item_interface_plan import (
    collect_interface_plan_validation_issues,
    validate_interface_intent_plan,
    validate_interface_plan_protocol,
    validate_interface_patch_protocol,
)
from backend.services.platform_io_contract import (
    parse_runtime_output_mappings,
    project_and_commit_skill_outputs,
    render_runtime_output_mapping,
    resolve_runtime_output_transform,
)


def _item():
    return {
        "target_file": "scripts/unit.py", "role": "script", "purpose": "produce result",
        "inputs": [],
        "outputs": [{"port_id": "result", "role": "runtime_output", "contract": {"type": "object"}}],
        "default_values": {}, "required_capabilities": [], "constraints": [],
    }


def _platform():
    return {"platform_skill_boundary": {
        "final_output_fields": [{
            "name": "text", "semantic_type": "text",
            "accepted_source_types": ["text"], "allowed_transforms": ["json_serialize"],
            "value_schema": {"type": "string"},
        }],
        "required_final_output_fields": ["text"],
    }}


def _interface(**extra):
    return {"interface_id": "I1", "kind": "member_to_platform",
            "source_member": "scripts/unit.py", "source_output": "result",
            "target_platform_output": "text", "goal": "deliver", **extra}


def test_interface_contract_without_transform():
    plan = {"interfaces": [_interface()]}
    assert validate_interface_intent_plan(plan=plan, function_items=[_item()]) == plan
    assert collect_interface_plan_validation_issues(
        plan=plan, function_items=[_item()], platform_contract=_platform()) == []


def test_legacy_interface_transform_is_ignored_during_normalization():
    assert validate_interface_plan_protocol(
        {"interfaces": [_interface(transform="json_serialize")]}
    ) == {"interfaces": [_interface()]}


def test_nonexistent_source_still_fails():
    plan = {"interfaces": [{**_interface(), "source_output": "missing"}]}
    with pytest.raises(Exception):
        validate_interface_intent_plan(plan=plan, function_items=[_item()])


def test_runtime_binding_resolves_representation_adaptation():
    assert resolve_runtime_output_transform(
        source_type="object", target_type="text", allowed_transforms=["json_serialize"]
    ) == "json_serialize"
    with pytest.raises(RuntimeError):
        resolve_runtime_output_transform(source_type="boolean", target_type="text")


def test_portable_skill_mapping_executes_json_serialization_without_creator_graph():
    record = render_runtime_output_mapping(
        "scripts/unit.py", {"text": ["result"]},
    )
    skill_text = f"# Installed skill\n{record}\n```bash\npython scripts/unit.py '{{}}'\n```"

    assert parse_runtime_output_mappings(skill_text) == [{
        "script": "scripts/unit.py", "source": "result", "target": "text",
    }]
    assert project_and_commit_skill_outputs(
        _platform(), skill_text,
        {"scripts/unit.py": {"result": [{"title": "项目概述"}]}},
    ) == {"text": '[{"title": "项目概述"}]'}


def test_portable_skill_mapping_rejects_unexecutable_transform():
    record = render_runtime_output_mapping(
        "scripts/unit.py", {"text": ["result"]},
    )
    with pytest.raises(RuntimeError, match="no runtime output adaptation"):
        project_and_commit_skill_outputs(
            _platform(), record,
            {"scripts/unit.py": {"result": True}},
        )


def test_portable_missing_stdout_attribution_uses_artifact_binding():
    violation = e2e._portable_terminal_runtime_contract_violation(
        mappings=[{"script": "scripts/unit.py", "source": "result", "target": "text"}],
        completed_outputs={"scripts/unit.py": {"other": "value"}},
    )
    assert violation == {
        "target_file": "scripts/unit.py",
        "output_name": "result",
        "sink_name": "text",
        "expected": {"output_present": True},
        "observed": {"output_present": False, "stdout_keys": ["other"]},
    }


def test_repair_protocol_does_not_allow_transform_replacement():
    with pytest.raises(Exception):
        validate_interface_patch_protocol({"operations": [{
            "op": "replace_transform", "interface_id": "I1",
            "reason": "not interface responsibility", "transform": "json_serialize",
        }]})
