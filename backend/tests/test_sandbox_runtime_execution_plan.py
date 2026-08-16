import asyncio
import copy
from pathlib import Path

import pytest

from backend.routers.sandbox.runtime_execution_plan import (
    VERSION, RuntimePlanError, _lookup, resolve_binding,
    resolve_step_bindings, validate_runtime_execution_plan,
)
from backend.routers.sandbox import workflow_dataflow


def schema(outputs=None, defaults=None):
    return {"entries": [{
        "script_path": "scripts/run.py", "inputs": ["value"],
        "optional_inputs": [], "command_keys": ["value"],
        "outputs": ["result"] if outputs is None else outputs,
        "default_values": defaults or {}, "command": "python scripts/run.py '{\"value\":\"{{value}}\"}'",
    }]}


def plan(binding=None, *, outputs=None):
    step = {"step_id": "step_1", "script_path": "scripts/run.py",
            "description": "display only", "bindings": {}}
    if binding is not None:
        step["bindings"]["value"] = binding
    if outputs is not None:
        step["expected_outputs"] = outputs
    return {"version": VERSION, "steps": [step]}


def test_runtime_plan_supports_input_files_index_path():
    assert _lookup({"input_files": [{"path": "inputs/a.dat"}]}, "input_files.0.path") == "inputs/a.dat"


def test_runtime_plan_supports_nested_list_path():
    assert _lookup({"payload": {"items": [[{"name": "x"}]]}}, "payload.items.0.0.name") == "x"


@pytest.mark.parametrize("path", ["files.nope.path", "files.2.path"])
def test_runtime_plan_rejects_invalid_or_out_of_range_list_index(path):
    with pytest.raises(KeyError):
        _lookup({"files": [{"path": "a"}]}, path)


def test_runtime_plan_structured_field_precedes_derived_value():
    canonical = validate_runtime_execution_plan(
        plan({"source_type": "derived_from_user_input", "value": 5}), schema(), {"fields": {"value": 3}}
    )
    assert canonical["steps"][0]["bindings"]["value"] == {"source_type": "envelope", "source": "fields.value"}


def test_runtime_plan_resolves_existing_resource():
    catalog = [{"resource_handle": "resource:0", "path": "references/a"}]
    binding = {"source_type": "resource", "resource_handle": "resource:0"}
    s = schema(); s["entries"][0]["inputs"] = ["value"]
    canonical = validate_runtime_execution_plan(plan(binding), s, {}, catalog)
    assert resolve_step_bindings(canonical["steps"][0], {}, {"steps": {}}, catalog)["value"]["path"] == "references/a"


def test_runtime_plan_rejects_missing_resource():
    with pytest.raises(RuntimePlanError) as caught:
        validate_runtime_execution_plan(plan({"source_type": "resource", "resource_handle": "resource:no"}), schema(), {}, [])
    assert caught.value.code == "runtime_plan_missing_resource"


def test_runtime_plan_default_is_canonical_from_schema():
    canonical = validate_runtime_execution_plan(
        plan({"source_type": "default", "value": 99}), schema(defaults={"value": 3}), {}
    )
    assert canonical["steps"][0]["bindings"]["value"]["value"] == 3


def test_runtime_plan_rejects_unknown_script_and_parameter():
    bad = plan({"source_type": "user_input", "value": 1}); bad["steps"][0]["script_path"] = "scripts/no.py"
    with pytest.raises(RuntimePlanError, match="runtime_plan_unknown_script"):
        validate_runtime_execution_plan(bad, schema())
    bad = plan({"source_type": "user_input", "value": 1}); bad["steps"][0]["bindings"]["extra"] = {"source_type": "user_input", "value": 2}
    with pytest.raises(RuntimePlanError, match="runtime_plan_unknown_parameter"):
        validate_runtime_execution_plan(bad, schema())


def test_runtime_plan_rejects_unknown_output_and_future_reference():
    s = schema(); s["entries"].append({**s["entries"][0], "script_path": "scripts/two.py"})
    p = {"version": VERSION, "steps": [
        {"step_id": "one", "script_path": "scripts/run.py", "bindings": {"value": {"source_type": "step_output", "step_id": "two", "output": "result"}}},
        {"step_id": "two", "script_path": "scripts/two.py", "bindings": {"value": {"source_type": "user_input", "value": 1}}},
    ]}
    with pytest.raises(RuntimePlanError, match="runtime_plan_future_reference"):
        validate_runtime_execution_plan(p, s)
    p["steps"][0]["bindings"]["value"]["step_id"] = "missing"
    with pytest.raises(RuntimePlanError, match="runtime_plan_invalid_dependency"):
        validate_runtime_execution_plan(p, s)


def test_runtime_plan_multiple_steps_same_script_use_step_id_context():
    p = {"version": VERSION, "steps": [
        {"step_id": "one", "script_path": "scripts/run.py", "bindings": {"value": {"source_type": "user_input", "value": 1}}},
        {"step_id": "two", "script_path": "scripts/run.py", "bindings": {"value": {"source_type": "step_output", "step_id": "one", "output": "result"}}},
    ]}
    canonical = validate_runtime_execution_plan(p, schema())
    assert resolve_step_bindings(canonical["steps"][1], {}, {"steps": {"one": {"result": "real"}}, "result": "flat"})["value"] == "real"


def test_runtime_plan_reports_missing_required_input():
    canonical = validate_runtime_execution_plan(plan(), schema(), {})
    assert canonical["missing_required_inputs"][0]["input"] == "value"


async def execute(monkeypatch, tmp_path, result, *, outputs=None):
    s = schema(outputs=outputs)
    p = plan({"source_type": "user_input", "value": 1}, outputs=outputs)
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", lambda *a, **k: (result, []))
    return await workflow_dataflow._execute_runtime_plan(
        execution_root=tmp_path, action_schema=s, runtime_plan=p, input_envelope={}
    )


@pytest.mark.parametrize("stdout", ["not-json", "[]"])
def test_runtime_step_rejects_invalid_json_stdout(monkeypatch, tmp_path, stdout):
    with pytest.raises(RuntimePlanError, match="runtime_plan_output_contract_mismatch"):
        asyncio.run(execute(monkeypatch, tmp_path, {"success": True, "stdout": stdout, "output_files": []}))


def test_runtime_step_rejects_missing_expected_output(monkeypatch, tmp_path):
    with pytest.raises(RuntimePlanError, match="missing output: result"):
        asyncio.run(execute(monkeypatch, tmp_path, {"success": True, "stdout": "{}", "output_files": []}))


def test_runtime_step_allows_extra_stdout_fields(monkeypatch, tmp_path):
    result = asyncio.run(execute(monkeypatch, tmp_path, {"success": True, "stdout": '{"result":1,"extra":2}', "output_files": []}))
    assert result["context"]["steps"]["step_1"]["extra"] == 2


def test_runtime_step_allows_artifact_only_empty_stdout(monkeypatch, tmp_path):
    result = asyncio.run(execute(monkeypatch, tmp_path, {"success": True, "stdout": "", "output_files": [{"path": "out.bin"}]}, outputs=[]))
    assert result["success"] and result["output_files"] == [{"path": "out.bin"}]


def test_legacy_react_rejects_unknown_parameter(monkeypatch, tmp_path):
    async def fake(*args, **kwargs):
        return '{"tool_call":{"script":"scripts/run.py","params":{"invented":1}}}'
    monkeypatch.setattr(workflow_dataflow, "complete_chat_once", fake)
    with pytest.raises(RuntimePlanError, match="runtime_plan_unknown_parameter"):
        asyncio.run(workflow_dataflow._execute_workflow_with_react_loop(
            execution_root=tmp_path, action_schema=schema(),
            step_plan={"steps": [{"script_path": "scripts/run.py", "description": "x"}]},
            user_context={},
        ))


def test_runtime_plan_resource_catalog_reaches_execution(monkeypatch, tmp_path):
    catalog = [{"resource_handle": "resource:0", "path": "references/source"}]
    captured = {}
    def fake(task, *args, **kwargs):
        import json, shlex
        captured.update(json.loads(shlex.split(task["command"])[2]))
        return {"success": True, "stdout": '{"result":true}', "output_files": []}, []
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", fake)
    runtime_plan = plan({"source_type": "resource", "resource_handle": "resource:0"})
    result = asyncio.run(workflow_dataflow._execute_skill_workflow(
        execution_root=tmp_path, action_schema=schema(), user_context={},
        dataflow_plan=runtime_plan, resource_catalog=catalog,
    ))
    assert result["success"] and captured["value"]["resource_handle"] == "resource:0"


def test_confirmed_runtime_plan_does_not_replan(monkeypatch, tmp_path):
    async def forbidden(*args, **kwargs):
        raise AssertionError("planner must not be called for a supplied confirmed plan")
    monkeypatch.setattr(workflow_dataflow, "_plan_workflow_steps_with_model", forbidden)
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", lambda *a, **k: (
        {"success": True, "stdout": '{"result":1}', "output_files": []}, []
    ))
    confirmed = plan({"source_type": "user_input", "value": 1})
    result = asyncio.run(workflow_dataflow._execute_skill_workflow(
        execution_root=tmp_path, action_schema=schema(), user_context={}, dataflow_plan=confirmed,
    ))
    assert result["runtime_plan"] == validate_runtime_execution_plan(confirmed, schema(), {})


def test_runtime_plan_rejects_cycle():
    p = {"version": VERSION, "steps": [
        {"step_id": "one", "script_path": "scripts/run.py", "bindings": {"value": {"source_type": "user_input", "value": 1}}, "depends_on": ["two"]},
        {"step_id": "two", "script_path": "scripts/run.py", "bindings": {"value": {"source_type": "user_input", "value": 2}}, "depends_on": ["one"]},
    ]}
    with pytest.raises(RuntimePlanError, match="runtime_plan_cycle"):
        validate_runtime_execution_plan(p, schema())


def test_runtime_plan_foreach_partial_failure_is_failure(monkeypatch, tmp_path):
    s = {"entries": [
        {**schema()["entries"][0], "outputs": ["items"]},
        {**schema()["entries"][0], "script_path": "scripts/item.py", "outputs": ["result"],
         "command": "python scripts/item.py '{\"value\":\"{{value}}\"}'"},
    ]}
    p = {"version": VERSION, "steps": [
        {"step_id": "source", "script_path": "scripts/run.py", "bindings": {"value": {"source_type": "user_input", "value": 1}}},
        {"step_id": "items", "script_path": "scripts/item.py",
         "bindings": {"value": {"source_type": "step_output", "step_id": "source", "output": "items"}},
         "foreach": {"source_type": "step_output", "step_id": "source", "output": "items"}},
    ]}
    calls = 0
    def fake(*args, **kwargs):
        nonlocal calls; calls += 1
        if calls == 1:
            return {"success": True, "stdout": '{"items":[1,2]}', "output_files": []}, []
        if calls == 2:
            return {"success": True, "stdout": '{"result":1}', "output_files": [{"path": "partial"}]}, []
        return {"success": False, "stdout": "", "stderr": "failed", "output_files": []}, []
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", fake)
    result = asyncio.run(workflow_dataflow._execute_runtime_plan(
        execution_root=tmp_path, action_schema=s, runtime_plan=p, input_envelope={}
    ))
    assert not result["success"]
    assert result["failed_instance_index"] == 1
    assert result["output_files"] == [{"path": "partial"}]


def test_runtime_planner_receives_safe_resource_catalog(monkeypatch, tmp_path):
    captured = {}
    async def fake(messages, model):
        captured["messages"] = messages
        return '{"version":"sandbox-runtime-plan/v1","steps":[{"step_id":"step_1","script_path":"scripts/run.py","bindings":{"value":{"source_type":"user_input","value":1}}}]}'
    monkeypatch.setattr(workflow_dataflow, "complete_chat_once", fake)
    catalog = [{
        "resource_handle": "resource:0", "path": "references/host-only.txt",
        "kind": "references", "title": "safe", "allowed_actions": ["read_resource"],
        "usage_hint": "use it", "host_secret": "must-not-leak",
    }]
    asyncio.run(workflow_dataflow._plan_workflow_steps_with_model(
        execution_root=tmp_path, action_schema=schema(), user_context={}, resource_catalog=catalog,
        reference_texts={"references/semantic.md": "semantic guidance"},
    ))
    prompt = "\n".join(str(message["content"]) for message in captured["messages"])
    assert "display_path" in prompt and "references/host-only.txt" in prompt
    assert "host_secret" not in prompt and "semantic guidance" in prompt


def test_runtime_plan_preview_count_and_checklist_source_are_runtime_steps():
    runtime_plan = validate_runtime_execution_plan({"version": VERSION, "steps": [
        {"step_id": "one", "script_path": "scripts/run.py", "description": "first",
         "bindings": {"value": {"source_type": "user_input", "value": 1}}},
        {"step_id": "two", "script_path": "scripts/run.py", "description": "second",
         "bindings": {"value": {"source_type": "step_output", "step_id": "one", "output": "result"}}},
    ]}, schema())
    preview = workflow_dataflow._runtime_plan_preview(runtime_plan)
    assert len(preview["steps"]) == 2
    assert [step["script_path"] for step in preview["steps"]] == ["scripts/run.py", "scripts/run.py"]
    assert preview["steps"][1]["bindings"]["value"] == "one.result"


def test_confirm_uses_revalidated_canonical_plan_without_replanning(monkeypatch, tmp_path):
    import time
    from backend.routers.chat_models import Message
    from backend.routers.sandbox.io_manifest import SandboxChatRequest
    from backend.routers.sandbox import stream_pipeline

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("", encoding="utf-8")
    request = SandboxChatRequest(messages=[Message(role="user", content="go")])
    pending_plan = plan({"source_type": "default", "value": 99})
    action_schema = schema(defaults={"value": 3})
    stream_pipeline._pending_plans["confirm-canonical"] = {
        "skill_context": {"execution_root": tmp_path}, "request": request,
        "runtime_execution_plan": pending_plan, "resource_catalog": [], "ts": time.time(),
    }
    captured = {}
    monkeypatch.setattr(stream_pipeline, "_build_runtime_action_schema", lambda *a, **k: action_schema)
    monkeypatch.setattr(stream_pipeline, "_make_stream", lambda context, req: captured.update(context) or "stream")
    result = asyncio.run(stream_pipeline.confirm_plan_execution(
        "skill", stream_pipeline.PlanConfirmRequest(plan_id="confirm-canonical")
    ))
    assert result == "stream"
    assert captured["confirmed_runtime_plan"]["steps"][0]["bindings"]["value"]["value"] == 3


def test_confirm_rejects_plan_with_missing_inputs(monkeypatch, tmp_path):
    import time
    from fastapi import HTTPException
    from backend.routers.chat_models import Message
    from backend.routers.sandbox.io_manifest import SandboxChatRequest
    from backend.routers.sandbox import stream_pipeline

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("", encoding="utf-8")
    stream_pipeline._pending_plans["confirm-missing"] = {
        "skill_context": {"execution_root": tmp_path},
        "request": SandboxChatRequest(messages=[Message(role="user", content="go")]),
        "runtime_execution_plan": plan(), "resource_catalog": [], "ts": time.time(),
    }
    monkeypatch.setattr(stream_pipeline, "_build_runtime_action_schema", lambda *a, **k: schema())
    with pytest.raises(HTTPException, match="缺少必要输入") as caught:
        asyncio.run(stream_pipeline.confirm_plan_execution(
            "skill", stream_pipeline.PlanConfirmRequest(plan_id="confirm-missing")
        ))
    assert caught.value.status_code == 400
