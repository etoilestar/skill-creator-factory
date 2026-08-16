import asyncio

import pytest

from backend.routers.sandbox import workflow_dataflow
from backend.routers.sandbox.adaptive_runtime import (
    POLICY_VERSION, validate_adaptive_policy,
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
    assert result["output_files"] == [{"path": "partial.txt"}]


def test_adaptive_policy_rejects_forbidden_and_duplicate_checkpoints():
    with pytest.raises(RuntimePlanError, match="forbidden"):
        validate_adaptive_policy({"version": POLICY_VERSION, "checkpoints": [
            {"after_step_id": "a", "reason": "x", "command": "bad"}]}, plan())
    with pytest.raises(RuntimePlanError, match="duplicate"):
        validate_adaptive_policy(policy("a", "a"), plan())
