import json

import pytest

from backend.services.creator import e2e
from backend.services.runtime.runtime_io_mapping_planner import (
    DEFAULT_RUNTIME_IO_CAPABILITIES,
    RuntimeIOMappingError,
    RuntimeIOMappingPlanner,
    adapt_runtime_output_before_validation,
    execute_runtime_io_mapping,
)


async def _run_case(tmp_path, source_schema, target_schema, value, operation):
    seen = {}

    async def model(messages):
        seen["request"] = json.loads(messages[1]["content"])
        return {
            "source": {"member": "asserter.py", "output": "report_json"},
            "target": {"sink": target_schema["name"]},
            "operation": operation,
        }

    mapping = await RuntimeIOMappingPlanner(model).plan(
        frozen_interface_contract={"contract_id": "frozen-524"},
        function_output_schema=source_schema,
        actual_runtime_output=value,
        platform_output_contract=target_schema,
        runtime_capabilities=DEFAULT_RUNTIME_IO_CAPABILITIES,
        source_member="asserter.py",
        source_output="report_json",
        target_sink=target_schema["name"],
    )
    result = execute_runtime_io_mapping(
        mapping, value, session_dir=tmp_path, runtime_capabilities=DEFAULT_RUNTIME_IO_CAPABILITIES
    )
    assert seen["request"]["source_schema"] == source_schema
    assert seen["request"]["target_schema"] == target_schema
    assert json.loads((tmp_path / "runtime_io_mapping.json").read_text())["operation"] == operation
    return mapping, result


@pytest.mark.asyncio
async def test_object_is_model_mapped_to_file_outputs(tmp_path):
    mapping, result = await _run_case(
        tmp_path, {"type": "object"}, {"name": "file_outputs", "type": "array"},
        {"passed": True}, "serialize_json_file",
    )
    assert mapping.operation == "serialize_json_file"
    assert json.loads(open(result[0], encoding="utf-8").read()) == {"passed": True}


@pytest.mark.asyncio
async def test_object_is_model_mapped_to_text(tmp_path):
    mapping, result = await _run_case(
        tmp_path, {"type": "object"}, {"name": "text", "type": "string"},
        {"passed": True}, "json_stringify",
    )
    assert mapping.operation == "json_stringify"
    assert json.loads(result) == {"passed": True}


@pytest.mark.asyncio
async def test_string_array_is_model_mapped_by_direct_pass(tmp_path):
    value = ["outputs/a.txt"]
    mapping, result = await _run_case(
        tmp_path, {"type": "array", "items": {"type": "string"}},
        {"name": "file_outputs", "type": "array"}, value, "direct_pass",
    )
    assert mapping.operation == "direct_pass"
    assert result is value


@pytest.mark.asyncio
async def test_model_cannot_select_undeclared_operation(tmp_path):
    async def model(_messages):
        return {"source": {"member": "a.py", "output": "out"},
                "target": {"sink": "text"}, "operation": "business_magic"}

    with pytest.raises(RuntimeIOMappingError, match="undeclared"):
        await RuntimeIOMappingPlanner(model).plan(
            frozen_interface_contract={}, function_output_schema={"type": "object"},
            actual_runtime_output={}, platform_output_contract={"type": "string"},
            runtime_capabilities=DEFAULT_RUNTIME_IO_CAPABILITIES,
            source_member="a.py", source_output="out", target_sink="text",
        )


@pytest.mark.asyncio
async def test_adapter_runs_before_platform_validator(tmp_path):
    events = []

    async def model(_messages):
        events.append("plan")
        return {"source": {"member": "a.py", "output": "out"},
                "target": {"sink": "text"}, "operation": "json_stringify"}

    def validator(value):
        events.append("validate")
        assert value == '{"answer": 42}'

    result = await adapt_runtime_output_before_validation(
        planner=RuntimeIOMappingPlanner(model), value={"answer": 42}, session_dir=tmp_path,
        validator=validator, frozen_interface_contract={"frozen": True},
        function_output_schema={"type": "object"},
        platform_output_contract={"name": "text", "type": "string"},
        runtime_capabilities=DEFAULT_RUNTIME_IO_CAPABILITIES,
        source_member="a.py", source_output="out", target_sink="text",
    )
    assert result == '{"answer": 42}'
    assert events == ["plan", "validate"]


@pytest.mark.asyncio
async def test_e2e_mapping_failure_can_only_replan_session_mapping():
    failure = "E2E_STRUCTURED_FAILURE=" + json.dumps({
        "target_file": "/tmp/session/runtime_io_mapping.json",
        "details": {
            "failure_code": "runtime_io_mapping_failure",
            "runtime_mapping_path": "/tmp/session/runtime_io_mapping.json",
        },
    })
    result = await e2e._repair_existing_file_for_e2e_failure(
        skill_name="does-not-need-to-exist",
        target_path="/tmp/session/runtime_io_mapping.json",
        e2e_errors=[failure],
    )
    assert result["status"] == "runtime_io_mapping_replan_required"
    assert result["next_target"] == "RUNTIME_IO_MAPPING"
    assert result["interface_contract_mutable"] is False
