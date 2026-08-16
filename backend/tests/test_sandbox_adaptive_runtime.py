import asyncio
import json
import shlex

import pytest

from backend.routers.sandbox import workflow_dataflow
from backend.routers.sandbox.adaptive_runtime import (
    POLICY_VERSION, _decide_after_observation, _plan_adaptive_policy_with_model,
    validate_adaptive_policy,
    validate_runtime_plan_revision,
)
from backend.routers.sandbox.runtime_execution_plan import RuntimePlanError, VERSION, validate_runtime_execution_plan


def schema():
    return {"entries": [{
        "script_path": f"scripts/{name}.py", "inputs": ["value"], "optional_inputs": [],
        "command_keys": ["value"], "outputs": ["result"], "default_values": {},
        "command": f"python scripts/{name}.py '{{\"value\":\"{{{{value}}}}\"}}'",
    } for name in ("a", "b", "c", "d", "e")]}


def plan(names=("a", "b", "c")):
    return {"version": VERSION, "steps": [{
        "step_id": name, "script_path": f"scripts/{name}.py", "description": name,
        "bindings": {"value": {"source_type": "user_input", "value": name}},
    } for name in names]}


def policy(*ids):
    return {"version": POLICY_VERSION, "checkpoints": [
        {"after_step_id": step_id, "reason": "observe actual result"} for step_id in ids
    ]}


async def run(monkeypatch, tmp_path, *, runtime_plan=None, adaptive_policy=None, decisions=(), executor=None):
    calls = []
    decision_iter = iter(decisions)
    async def decide(**kwargs):
        calls.append(kwargs)
        return next(decision_iter)
    monkeypatch.setattr(workflow_dataflow, "_decide_after_observation", decide)
    executed = []
    def default_executor(task, *args, **kwargs):
        script = next(name for name in ("a", "b", "c", "d", "e") if f"scripts/{name}.py" in task["command"])
        executed.append(script)
        return {"success": True, "stdout": f'{{"result":"{script}"}}', "output_files": []}, []
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", executor or default_executor)
    result = await workflow_dataflow._execute_runtime_plan(
        execution_root=tmp_path, action_schema=schema(), runtime_plan=runtime_plan or plan(),
        input_envelope={"user_request": "do it"}, adaptive_policy=adaptive_policy,
    )
    return result, calls, executed


def test_adaptive_runtime_skips_model_without_checkpoint(monkeypatch, tmp_path):
    result, calls, executed = asyncio.run(run(monkeypatch, tmp_path, adaptive_policy=policy()))
    assert result["success"] and executed == ["a", "b", "c"] and calls == []


def test_adaptive_runtime_checkpoint_continue(monkeypatch, tmp_path):
    result, calls, executed = asyncio.run(run(monkeypatch, tmp_path, adaptive_policy=policy("b"),
        decisions=[{"action": "continue", "reason": "continue deterministically"}]))
    assert executed == ["a", "b", "c"] and len(calls) == 1
    assert result["adaptive_trace"][0]["decision"] == "continue"


def test_adaptive_runtime_replans_remaining_steps(monkeypatch, tmp_path):
    revised = plan(("a", "b", "e", "d"))
    result, calls, executed = asyncio.run(run(monkeypatch, tmp_path, runtime_plan=plan(("a", "b", "c", "d")),
        adaptive_policy=policy("b"), decisions=[{"action": "replan_remaining", "reason": "change suffix", "revised_plan": revised}]))
    assert executed == ["a", "b", "e", "d"]
    assert result["runtime_plan"]["steps"][2]["step_id"] == "e"


def test_adaptive_runtime_rejects_modified_completed_step():
    original = validate_runtime_execution_plan(plan(("a", "b")), schema())
    revised = plan(("a", "b"))
    revised["steps"][0]["script_path"] = "scripts/e.py"
    with pytest.raises(RuntimePlanError) as caught:
        validate_runtime_plan_revision(original, revised, ["a"], schema())
    assert caught.value.code == "completed_prefix_modified"


def test_adaptive_runtime_rejects_unknown_script_revision():
    original = validate_runtime_execution_plan(plan(("a", "b")), schema())
    revised = plan(("a", "b"))
    revised["steps"][1]["script_path"] = "scripts/not_exist.py"
    with pytest.raises(RuntimePlanError, match="runtime_plan_unknown_script"):
        validate_runtime_plan_revision(original, revised, ["a"], schema())


def test_adaptive_runtime_retry_is_bounded(monkeypatch, tmp_path):
    attempts = 0
    def executor(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return ({"success": False, "stderr": "real failure", "returncode": 2, "output_files": []}, [])
    result, calls, _ = asyncio.run(run(monkeypatch, tmp_path, runtime_plan=plan(("a",)), adaptive_policy=policy(),
        decisions=[{"action": "retry_current", "reason": "retry"}, {"action": "retry_current", "reason": "again"}],
        executor=executor))
    assert not result["success"] and attempts == 2 and len(calls) == 2
    assert result["stderr"] == "real failure"


def test_adaptive_runtime_ask_user_keeps_partial_artifacts(monkeypatch, tmp_path):
    def executor(*args, **kwargs):
        return {"success": False, "stderr": "missing detail", "returncode": 1,
                "stdout": "", "output_files": [{"path": "partial.txt"}]}, []
    result, _, _ = asyncio.run(run(monkeypatch, tmp_path, runtime_plan=plan(("a",)), adaptive_policy=policy(),
        decisions=[{"action": "ask_user", "reason": "need format", "missing": ["format"]}], executor=executor))
    assert result["mode"] == "ask_user" and result["missing"] == ["format"]
    assert result["output_files"] == []
    assert result["attempt_output_files"] == [{"path": "partial.txt"}]


def test_adaptive_policy_rejects_forbidden_and_duplicate_checkpoints():
    with pytest.raises(RuntimePlanError, match="forbidden"):
        validate_adaptive_policy({"version": POLICY_VERSION, "checkpoints": [
            {"after_step_id": "a", "reason": "x", "command": "bad"}]}, plan())
    with pytest.raises(RuntimePlanError, match="duplicate"):
        validate_adaptive_policy(policy("a", "a"), plan())


@pytest.mark.parametrize("response", [RuntimeError("offline"), "not-json"])
def test_adaptive_policy_planner_failure_falls_back_to_empty_policy(monkeypatch, response):
    async def complete(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr("backend.routers.sandbox.adaptive_runtime.complete_chat_once", complete)
    result = asyncio.run(_plan_adaptive_policy_with_model(
        user_request="run", runtime_plan=plan(), action_schema=schema(),
    ))
    assert result == policy()


def test_adaptive_policy_invalid_json_does_not_break_deterministic_execution(monkeypatch, tmp_path):
    async def complete(*args, **kwargs):
        return "invalid"
    monkeypatch.setattr("backend.routers.sandbox.adaptive_runtime.complete_chat_once", complete)
    adaptive_policy = asyncio.run(_plan_adaptive_policy_with_model(
        user_request="run", runtime_plan=plan(), action_schema=schema(),
    ))
    result, calls, executed = asyncio.run(run(monkeypatch, tmp_path, adaptive_policy=adaptive_policy))
    assert result["success"] and calls == [] and executed == ["a", "b", "c"]


def test_missing_runtime_inputs_skip_adaptive_planner(monkeypatch, tmp_path):
    missing_plan = plan(("a",))
    missing_plan["steps"][0]["bindings"] = {}
    async def planner(**kwargs):
        return validate_runtime_execution_plan(missing_plan, schema(), {})
    async def forbidden(**kwargs):
        raise AssertionError("adaptive planner must be skipped")
    monkeypatch.setattr(workflow_dataflow, "_plan_workflow_steps_with_model", planner)
    monkeypatch.setattr(workflow_dataflow, "_plan_adaptive_policy_with_model", forbidden)
    with pytest.raises(RuntimePlanError, match="runtime_plan_missing_input"):
        asyncio.run(workflow_dataflow._execute_skill_workflow(
            execution_root=tmp_path, action_schema=schema(), user_context={},
        ))


def test_adaptive_replan_refreshes_policy(monkeypatch, tmp_path):
    revised = plan(("a", "b", "e", "d"))
    refreshes = []
    async def refresh(**kwargs):
        refreshes.append(kwargs["runtime_plan"])
        return policy("e")
    monkeypatch.setattr(workflow_dataflow, "_plan_adaptive_policy_with_model", refresh)
    result, calls, executed = asyncio.run(run(
        monkeypatch, tmp_path, runtime_plan=plan(("a", "b", "c", "d")), adaptive_policy=policy("b", "c"),
        decisions=[{"action": "replan_remaining", "reason": "revise", "revised_plan": revised},
                   {"action": "continue", "reason": "accept e"}],
    ))
    assert result["success"] and executed == ["a", "b", "e", "d"]
    assert len(refreshes) == 1 and len(calls) == 2


def test_adaptive_replan_policy_failure_falls_back_to_empty_policy(monkeypatch, tmp_path):
    revised = plan(("a", "b", "e", "d"))
    async def unavailable(*args, **kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr("backend.routers.sandbox.adaptive_runtime.complete_chat_once", unavailable)
    result, calls, executed = asyncio.run(run(
        monkeypatch, tmp_path, runtime_plan=plan(("a", "b", "c", "d")), adaptive_policy=policy("b"),
        decisions=[{"action": "replan_remaining", "reason": "revise", "revised_plan": revised}],
    ))
    assert result["success"] and len(calls) == 1 and executed == ["a", "b", "e", "d"]


def test_adaptive_replan_missing_input_asks_user(monkeypatch, tmp_path):
    revised = plan(("a", "b", "e"))
    revised["steps"][2]["bindings"] = {}
    result, _, executed = asyncio.run(run(
        monkeypatch, tmp_path, runtime_plan=plan(("a", "b", "c")), adaptive_policy=policy("b"),
        decisions=[{"action": "replan_remaining", "reason": "need input", "revised_plan": revised}],
    ))
    assert result["mode"] == "ask_user" and result["success"] is None
    assert result["paused_for_user"] and result["missing"][0]["step_id"] == "e"
    assert executed == ["a", "b"]


def test_adaptive_retry_uses_validated_binding_override(monkeypatch, tmp_path):
    values = []
    def executor(task, *args, **kwargs):
        value = json.loads(shlex.split(task["command"])[2])["value"]
        values.append(value)
        return {"success": len(values) > 1, "stderr": "transient", "returncode": 1,
                "stdout": '{"result":"ok"}' if len(values) > 1 else "", "output_files": []}, []
    result, _, _ = asyncio.run(run(
        monkeypatch, tmp_path, runtime_plan=plan(("a",)), adaptive_policy=policy(), executor=executor,
        decisions=[{"action": "retry_current", "reason": "correct value",
                    "bindings": {"value": {"source_type": "user_input", "value": "fixed"}}}],
    ))
    assert result["success"] and values == ["a", "fixed"]


def test_adaptive_retry_rejects_unknown_parameter(monkeypatch, tmp_path):
    with pytest.raises(RuntimePlanError, match="runtime_plan_unknown_parameter"):
        asyncio.run(run(monkeypatch, tmp_path, runtime_plan=plan(("a",)), adaptive_policy=policy("a"),
            decisions=[{"action": "retry_current", "reason": "bad",
                        "bindings": {"unknown": {"source_type": "user_input", "value": 1}}}]))


def test_adaptive_retry_rejects_script_change(monkeypatch):
    async def complete(*args, **kwargs):
        return '{"action":"retry_current","reason":"bad","script_path":"scripts/e.py"}'
    monkeypatch.setattr("backend.routers.sandbox.adaptive_runtime.complete_chat_once", complete)
    with pytest.raises(RuntimePlanError, match="forbidden"):
        asyncio.run(_decide_after_observation(
            user_request="run", runtime_plan=plan(), completed_step_ids=[], safe_observation={},
            action_schema=schema(), adaptive_reason="x", trigger="checkpoint",
        ))


def test_adaptive_retry_does_not_accept_superseded_artifacts(monkeypatch, tmp_path):
    attempts = 0
    def executor(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return {"success": True, "stdout": f'{{"result":{attempts}}}',
                "output_files": [{"path": f"attempt-{attempts}.txt"}]}, []
    result, _, _ = asyncio.run(run(monkeypatch, tmp_path, runtime_plan=plan(("a",)),
        adaptive_policy=policy("a"), executor=executor,
        decisions=[{"action": "retry_current", "reason": "supersede"}]))
    assert result["output_files"] == [{"path": "attempt-2.txt"}]
    assert result["attempt_output_files"] == [{"path": "attempt-1.txt"}, {"path": "attempt-2.txt"}]
    assert [item["accepted"] for item in result["runtime_instances"]] == [False, True]


def test_checkpoint_decision_failure_falls_back_to_continue(monkeypatch, tmp_path):
    async def broken(**kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(workflow_dataflow, "_decide_after_observation", broken)
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", lambda *a, **k: (
        {"success": True, "stdout": '{"result":1}', "output_files": []}, []))
    result = asyncio.run(workflow_dataflow._execute_runtime_plan(
        execution_root=tmp_path, action_schema=schema(), runtime_plan=plan(("a", "b")),
        input_envelope={}, adaptive_policy=policy("a")))
    assert result["success"] and result["adaptive_trace"][0]["decision"] == "fallback_continue"


def test_failure_decision_failure_preserves_real_failure(monkeypatch, tmp_path):
    async def broken(**kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(workflow_dataflow, "_decide_after_observation", broken)
    monkeypatch.setattr(workflow_dataflow, "_execute_single_task", lambda *a, **k: (
        {"success": False, "stderr": "real error", "returncode": 9, "output_files": []}, []))
    result = asyncio.run(workflow_dataflow._execute_runtime_plan(
        execution_root=tmp_path, action_schema=schema(), runtime_plan=plan(("a",)),
        input_envelope={}, adaptive_policy=policy()))
    assert not result["success"] and result["stderr"] == "real error"
    assert result["adaptive_trace"][0]["decision"] == "fallback_stop_failure"


def test_adaptive_decision_budget_counts_model_calls(monkeypatch, tmp_path):
    result, calls, executed = asyncio.run(run(
        monkeypatch, tmp_path, runtime_plan=plan(("a", "b", "c", "d", "e")),
        adaptive_policy=policy("a", "b", "c", "d", "e"),
        decisions=[{"action": "continue", "reason": "continue"}] * 4,
    ))
    assert result["success"] and executed == ["a", "b", "c", "d", "e"]
    assert len(calls) == 4 and len(result["adaptive_trace"]) == 4


def test_adaptive_stop_success_records_early_stop(monkeypatch, tmp_path):
    result, _, executed = asyncio.run(run(
        monkeypatch, tmp_path, adaptive_policy=policy("a"),
        decisions=[{"action": "stop_success", "reason": "already complete"}],
    ))
    assert result["success"] and executed == ["a"]
    assert result["stopped_early"] is True and result["stop_reason"] == "adaptive"
