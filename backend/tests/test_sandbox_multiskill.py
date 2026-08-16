import asyncio
import ast
from pathlib import Path

import pytest

from backend.routers.sandbox import multiskill_executor as executor
from backend.routers.sandbox import multiskill_plan as plans
from backend.routers.sandbox import multiskill_manager as manager


def plan(*steps):
    return {"version": plans.VERSION, "steps": list(steps), "missing_required_inputs": [], "warnings": []}


def step(step_id="s1", skill="one", task="do one", bindings=None, depends=None, **extra):
    return {"step_id": step_id, "skill_name": skill, "task": task,
            "bindings": bindings or {}, "depends_on": depends or [], **extra}


@pytest.fixture(autouse=True)
def governance(monkeypatch):
    def resolve(name, **kwargs):
        assert kwargs == {"mode": "sandbox", "require_visible": True, "require_executable": True}
        if name == "disabled":
            raise PermissionError(name)
        return {"name": name, "root_path": f"/skills/{name}"}
    monkeypatch.setattr(plans, "resolve_skill_record", resolve)
    monkeypatch.setattr(executor, "resolve_skill_record", resolve)


@pytest.mark.parametrize("field", plans.FORBIDDEN_FIELDS)
def test_multiskill_plan_rejects_tool_and_runtime_fields(field):
    with pytest.raises(plans.MultiSkillPlanError, match="multiskill_forbidden_field"):
        plans.validate_multiskill_plan(plan(step(**{field: "private"})), ["one"])


def test_multiskill_does_not_import_creator_tool_registry():
    for filename in ("multiskill_plan.py", "multiskill_executor.py"):
        tree = ast.parse((Path(executor.__file__).parent / filename).read_text())
        imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any("creator" in name or "tool_registry" in name for name in imported)


def test_multiskill_rejects_unknown_non_executable_future_channel_and_cycle():
    with pytest.raises(plans.MultiSkillPlanError, match="unknown_skill"):
        plans.validate_multiskill_plan(plan(step()), [])
    with pytest.raises(plans.MultiSkillPlanError, match="not_executable"):
        plans.validate_multiskill_plan(plan(step(skill="disabled")), ["disabled"])
    binding = {"input": {"source_type": "skill_result", "step_id": "s2", "channel": "text"}}
    with pytest.raises(plans.MultiSkillPlanError, match="future_skill_result"):
        plans.validate_multiskill_plan(plan(step(bindings=binding)), ["one"])
    bad = {"input": {"source_type": "skill_result", "step_id": "s1", "channel": "runtime_plan"}}
    with pytest.raises(plans.MultiSkillPlanError, match="result_channel"):
        plans.validate_multiskill_plan(plan(step(), step("s2", bindings=bad, depends=["s1"])), ["one"])
    with pytest.raises(plans.MultiSkillPlanError, match="invalid_dependency"):
        plans.validate_multiskill_plan(plan(step(depends=["s1"])), ["one"])


@pytest.mark.asyncio
async def test_multiskill_planner_can_choose_single_skill():
    result = await plans.plan_multiskill(user_request="one", input_envelope_summary={},
        activation_cards=[{"skill_name": "one"}], model_call=lambda *_: '{"mode":"single_skill","skill_name":"one"}')
    assert result == {"mode": "single_skill", "skill_name": "one"}


@pytest.mark.asyncio
async def test_multiskill_planner_builds_two_skill_plan():
    raw = '{"mode":"multi_skill","plan":{"version":"sandbox-multiskill-plan/v1","steps":[' \
          '{"step_id":"s1","skill_name":"one","task":"analyze","bindings":{},"depends_on":[]},' \
          '{"step_id":"s2","skill_name":"two","task":"present","bindings":{"payload":{"source_type":"skill_result","step_id":"s1","channel":"structured_outputs"}},"depends_on":["s1"]}]}}'
    result = await plans.plan_multiskill(user_request="both", input_envelope_summary={},
        activation_cards=[{"skill_name": "one"}, {"skill_name": "two"}], model_call=lambda *_: raw)
    assert result["mode"] == "multi_skill"
    assert [item["skill_name"] for item in result["plan"]["steps"]] == ["one", "two"]


def test_child_result_only_exposes_public_manifest():
    result = executor.normalize_child_skill_result({"success": True, "text": "safe", "runtime_plan": {"secret": 1},
        "context": {"steps": []}, "stdout": "private", "host_path": "/tmp/x"}, child_run_id="r", skill_name="one")
    assert set(result) == executor.PUBLIC_RESULT_FIELDS | {"child_run_id", "skill_name"}
    assert "private" not in str(result) and "secret" not in str(result)


@pytest.mark.asyncio
async def test_multiskill_invokes_runtime_and_passes_manifest_channels_serially():
    calls = []
    async def runtime(**kwargs):
        calls.append(kwargs)
        if kwargs["skill_name"] == "one":
            return {"success": True, "text": "analysis", "structured_outputs": {"summary": "short"},
                    "artifacts": [{"artifact_id": "a1"}], "output_files": [{"resource_id": "f1"}],
                    "runtime_plan": {"must": "not leak"}}
        return {"success": True, "text": "done", "structured_outputs": {}, "artifacts": [], "output_files": []}
    bindings = {
        "payload": {"source_type": "skill_result", "step_id": "s1", "channel": "structured_outputs", "path": "summary"},
        "resources": {"source_type": "skill_result", "step_id": "s1", "channel": "artifacts"},
        "input_files": {"source_type": "skill_result", "step_id": "s1", "channel": "output_files"},
    }
    result = await executor.execute_multiskill_plan(plan=plan(step(), step("s2", "two", "make slides", bindings, ["s1"])),
        activated_skill_names=["one", "two"], parent_envelope={"user_request": "original", "fields": {"x": 1}},
        single_skill_runtime=runtime)
    assert result["success"] is True and [call["skill_name"] for call in calls] == ["one", "two"]
    assert calls[0]["input_envelope"]["user_request"] == "do one"
    assert calls[1]["input_envelope"]["user_request"] == "make slides"
    assert calls[1]["input_envelope"]["payload"] == "short"
    assert calls[1]["input_envelope"]["resources"] == [{"artifact_id": "a1"}]
    assert calls[1]["input_envelope"]["input_files"] == [{"resource_id": "f1"}]
    assert "runtime_plan" not in str(result)
    assert result["skills"]["s1"]["child_run_id"].startswith("child_")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ask_user", "failed"])
async def test_child_pause_or_failure_stops_parent(kind):
    calls = []
    async def runtime(**kwargs):
        calls.append(kwargs)
        if kind == "ask_user":
            return {"mode": "ask_user", "text": "need file", "structured_outputs": {"missing": ["file"]}}
        return {"success": False, "status": "failed", "text": "bad"}
    result = await executor.execute_multiskill_plan(plan=plan(step(), step("s2", "two", depends=["s1"])),
        activated_skill_names=["one", "two"], parent_envelope={}, single_skill_runtime=runtime)
    assert len(calls) == 1
    if kind == "ask_user":
        assert result["mode"] == "ask_user" and result["paused_at_step_id"] == "s1"
    else:
        assert result["success"] is False and result["failed_skill_step_id"] == "s1"


@pytest.mark.asyncio
async def test_confirmed_multiskill_plan_executes_same_plan_without_planner():
    canonical = plan(step(task="confirmed task"))
    calls = []
    async def runtime(**kwargs):
        calls.append(kwargs)
        return {"success": True, "text": "ok"}
    result = await manager.run_multiskill_manager(user_request="original", parent_envelope={},
        activation_cards=[{"skill_name": "one"}], single_skill_runtime=runtime,
        confirmed_plan=canonical, model_call=lambda *_: pytest.fail("planner must not run"))
    assert result["success"] is True
    assert calls[0]["input_envelope"]["user_request"] == "confirmed task"


def test_plan_preview_stays_at_skill_and_manifest_level():
    canonical = plans.validate_multiskill_plan(plan(step(), step("s2", "two", "present", {
        "payload": {"source_type": "skill_result", "step_id": "s1", "channel": "structured_outputs", "path": "summary"}
    }, ["s1"])), ["one", "two"])
    preview = manager.preview_multiskill_plan(canonical)
    assert preview[1]["input_sources"] == [{"target": "payload", "source": "s1.structured_outputs.summary"}]
    assert "command" not in str(preview) and "script" not in str(preview)
