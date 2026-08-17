import asyncio
import ast
import json
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


def test_child_does_not_inherit_unbound_parent_fields():
    envelope = executor.build_child_input_envelope(step(), {
        "payload": {"secret": "x"}, "fields": {"secret": "x"},
        "options": {"foo": "bar"}, "resources": [{"secret": True}],
    }, {}, child_run_id="child-1")
    assert envelope["payload"] is None
    assert envelope["fields"] == {} and envelope["options"] == {} and envelope["resources"] == []
    assert envelope["user_request"] == envelope["input"] == envelope["text"] == "do one"


def test_child_does_not_receive_unbound_parent_files():
    envelope = executor.build_child_input_envelope(step(), {
        "input_files": [{"path": "inputs/private"}], "files": [{"path": "inputs/private"}],
        "_platform_input_root": "/private/host/root",
    }, {}, child_run_id="child-1")
    assert envelope["input_files"] == [] and envelope["files"] == []


@pytest.mark.asyncio
async def test_multiskill_invokes_runtime_and_passes_manifest_channels_serially():
    calls = []
    async def runtime(**kwargs):
        calls.append(kwargs)
        if kwargs["skill_name"] == "one":
            return {"success": True, "text": "analysis", "structured_outputs": [{"source": "stdout", "data": {"summary": "short"}}],
                    "artifacts": [{"artifact_id": "a1"}], "output_files": [],
                    "runtime_plan": {"must": "not leak"}}
        return {"success": True, "text": "done", "structured_outputs": [], "artifacts": [], "output_files": []}
    bindings = {
        "payload": {"source_type": "skill_result", "step_id": "s1", "channel": "structured_outputs", "path": "[0].data.summary"},
        "resources": {"source_type": "skill_result", "step_id": "s1", "channel": "artifacts"},
    }
    result = await executor.execute_multiskill_plan(plan=plan(step(), step("s2", "two", "make slides", bindings, ["s1"])),
        activated_skill_names=["one", "two"], parent_envelope={"user_request": "original", "fields": {"x": 1}},
        single_skill_runtime=runtime)
    assert result["success"] is True and [call["skill_name"] for call in calls] == ["one", "two"]
    assert calls[0]["input_envelope"]["user_request"] == "do one"
    assert calls[1]["input_envelope"]["user_request"] == "make slides"
    assert calls[1]["input_envelope"]["payload"] == "short"
    assert calls[1]["input_envelope"]["resources"] == [{"artifact_id": "a1"}]
    assert calls[1]["input_envelope"]["input_files"] == []
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
async def test_multiskill_missing_required_inputs_does_not_execute_children():
    calls = []
    missing_plan = plan(step())
    missing_plan["missing_required_inputs"] = [{"input": "document", "reason": "required"}]
    result = await executor.execute_multiskill_plan(plan=missing_plan,
        activated_skill_names=["one"], parent_envelope={},
        single_skill_runtime=lambda **kwargs: calls.append(kwargs))
    assert calls == []
    assert result["success"] is None and result["mode"] == "ask_user"
    assert result["missing"] == [{"input": "document", "reason": "required"}]


@pytest.mark.asyncio
async def test_multiskill_missing_required_inputs_returns_ask_user():
    missing_plan = plan(step())
    missing_plan["missing_required_inputs"] = ["document"]
    result = await manager.run_multiskill_manager(user_request="run", parent_envelope={},
        activation_cards=[{"skill_name": "one"}], confirmed_plan=missing_plan)
    assert result == {"success": None, "completed": False, "paused_for_user": True,
        "mode": "ask_user", "reason": "Multi-Skill plan requires additional input.",
        "missing": ["document"], "plan": plans.validate_multiskill_plan(missing_plan, ["one"])}


@pytest.mark.asyncio
async def test_multiskill_plan_mode_returns_plan_id_and_confirmation_uses_same_plan(monkeypatch):
    manager._pending_multiskill_plans.clear()
    counters = {"catalog": 0, "shortlist": 0, "planner": 0}
    def catalog(**kwargs): counters["catalog"] += 1; return [{"name": "one"}]
    async def shortlist(**kwargs): counters["shortlist"] += 1; return {"candidates": [{"skill_name": "one"}]}
    def activate(names): return [{"skill_name": name} for name in names]
    async def planner_call(*_):
        counters["planner"] += 1
        return '{"mode":"multi_skill","plan":{"version":"sandbox-multiskill-plan/v1","steps":[{"step_id":"s1","skill_name":"one","task":"same task","bindings":{},"depends_on":[]}]}}'
    calls = []
    async def runtime(**kwargs): calls.append(kwargs); return {"success": True, "text": "ok"}
    monkeypatch.setattr(manager, "build_multiskill_catalog", catalog)
    monkeypatch.setattr(manager, "_plan_skill_candidates_with_model", shortlist)
    monkeypatch.setattr(manager, "build_multiskill_activation_cards", activate)
    preview = await manager.run_multiskill_orchestration(user_request="run", parent_envelope={},
        execution_mode="plan", planner_model_call=planner_call)
    assert preview["mode"] == "plan" and preview["plan_id"].startswith("multiskill_")
    result = await manager.confirm_multiskill_plan(preview["plan_id"], single_skill_runtime=runtime,
        final_synthesizer=lambda **_: "done")
    assert result["success"] is True and calls[0]["input_envelope"]["user_request"] == "same task"
    assert counters == {"catalog": 1, "shortlist": 1, "planner": 1}


@pytest.mark.asyncio
async def test_single_skill_plan_mode_does_not_execute_and_confirmation_is_stable(monkeypatch):
    manager._pending_multiskill_plans.clear()
    counters = {"catalog": 0, "shortlist": 0, "planner": 0}
    monkeypatch.setattr(manager, "build_multiskill_catalog",
        lambda **_: (counters.__setitem__("catalog", counters["catalog"] + 1) or [{"name": "one"}]))
    async def shortlist(**kwargs):
        counters["shortlist"] += 1
        return {"candidates": [{"skill_name": "one"}]}
    monkeypatch.setattr(manager, "_plan_skill_candidates_with_model", shortlist)
    monkeypatch.setattr(manager, "build_multiskill_activation_cards",
        lambda names: [{"skill_name": name} for name in names])
    async def planner(*_):
        counters["planner"] += 1
        return '{"mode":"single_skill","skill_name":"one"}'
    calls = []
    async def runtime(**kwargs):
        calls.append(kwargs)
        return {"success": True, "structured_outputs": [], "artifacts": [], "output_files": []}
    preview = await manager.run_multiskill_orchestration(user_request="original", parent_envelope={
        "user_request": "original", "text": "original"}, execution_mode="plan",
        single_skill_runtime=runtime, planner_model_call=planner)
    assert calls == []
    assert preview["mode"] == "plan" and preview["selection_mode"] == "single_skill"
    assert preview["plan_id"].startswith("multiskill_") and preview["skill_name"] == "one"
    result = await manager.confirm_multiskill_plan(preview["plan_id"], single_skill_runtime=runtime,
        final_synthesizer=lambda **_: "final")
    assert len(calls) == 1 and result["skill_name"] == "one" and result["text"] == "final"
    assert counters == {"catalog": 1, "shortlist": 1, "planner": 1}
    assert set(result) >= {"skill_results", "skills", "artifacts", "output_files", "multi_skill_trace", "text"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    {"success": None, "mode": "ask_user", "paused_for_user": True,
     "completed": False, "reason": "need file", "missing": ["file"]},
    {"success": False, "status": "failed", "reason": "script failed"},
])
async def test_single_skill_ask_user_or_failure_does_not_synthesize(raw):
    async def runtime(**kwargs): return raw
    result = await manager.run_multiskill_manager(user_request="run", parent_envelope={
        "user_request": "run", "input": "run", "text": "run"},
        activation_cards=[{"skill_name": "one"}], confirmed_skill_name="one",
        single_skill_runtime=runtime,
        final_synthesizer=lambda **_: pytest.fail("non-success must not synthesize"))
    assert result["text"] == ""
    assert result["success"] is raw["success"]


def test_expired_multiskill_plan_is_rejected(monkeypatch):
    manager._pending_multiskill_plans.clear()
    manager._pending_multiskill_plans["old"] = {"created_at": 1}
    monkeypatch.setattr(manager.time, "time", lambda: 1 + manager._MULTISKILL_PLAN_EXPIRY_SECONDS + 1)
    with pytest.raises(manager.PendingMultiSkillPlanError, match="expired"):
        manager._take_multiskill_plan("old")


@pytest.mark.asyncio
async def test_confirmed_multiskill_plan_revalidates_governance(monkeypatch):
    manager._pending_multiskill_plans.clear()
    manager._pending_multiskill_plans["disabled"] = {
        "created_at": manager.time.time(), "plan": plan(step()),
        "user_request": "run", "parent_envelope": {}, "model": None,
    }
    monkeypatch.setattr(manager, "build_multiskill_activation_cards",
        lambda names: (_ for _ in ()).throw(PermissionError("skill_not_executable")))
    with pytest.raises(PermissionError, match="skill_not_executable"):
        await manager.confirm_multiskill_plan("disabled")


@pytest.mark.asyncio
async def test_multiskill_sse_bridges_parent_child_result_and_done(monkeypatch, tmp_path):
    from backend.routers.sandbox import stream_pipeline
    from backend.routers.sandbox.io_manifest import SandboxChatRequest
    monkeypatch.setattr(stream_pipeline.settings, "multiskill_uploads_path", tmp_path)
    monkeypatch.setattr(stream_pipeline, "_SSE_KEEPALIVE_INTERVAL", 0.001)
    async def orchestration(**kwargs):
        await kwargs["event_sink"]({"skill_started": {"step_id": "s1", "child_run_id": "c1"}})
        await kwargs["event_sink"]({"child_runtime_event": {"child_run_id": "c1", "event": {"step": "inner"}}})
        await asyncio.sleep(0.003)
        return {"success": True, "mode": "multi_skill", "text": "final",
                "multi_skill_trace": [{"step_id": "s1"}], "artifacts": [], "output_files": []}
    monkeypatch.setattr(stream_pipeline, "run_multiskill_orchestration", orchestration)
    response = await stream_pipeline.chat_in_multiskill_sandbox(
        SandboxChatRequest(messages=[{"role": "user", "content": "run"}]))
    body = "".join([chunk async for chunk in response.body_iterator])
    assert "skill_started" in body and "child_runtime_event" in body
    assert "multi_skill_trace" in body and "result_manifest" in body and '"answer": "final"' in body
    assert ": keepalive" in body and body.endswith("data: [DONE]\n\n")
    events = [json.loads(line.removeprefix("data: ")) for line in body.splitlines()
              if line.startswith("data: {")]
    manifest = next(event["result_manifest"] for event in events if "result_manifest" in event)
    assert set(manifest) == {"version", "structured_outputs", "artifacts"}


@pytest.mark.asyncio
async def test_multiskill_http_confirmation_uses_stored_plan_path(monkeypatch, tmp_path):
    from backend.routers.sandbox import stream_pipeline
    from backend.routers.sandbox.io_manifest import SandboxChatRequest
    monkeypatch.setattr(stream_pipeline.settings, "multiskill_uploads_path", tmp_path)
    seen = []
    async def confirm(plan_id, **kwargs):
        seen.append(plan_id)
        return {"success": True, "mode": "multi_skill", "artifacts": [], "output_files": []}
    monkeypatch.setattr(stream_pipeline, "confirm_multiskill_plan", confirm)
    monkeypatch.setattr(stream_pipeline, "run_multiskill_orchestration",
        lambda **_: pytest.fail("confirmation must not rediscover or replan"))
    response = await stream_pipeline.chat_in_multiskill_sandbox(SandboxChatRequest(
        messages=[{"role": "user", "content": "confirm"}], multiskill_plan_id="plan-1"))
    body = "".join([chunk async for chunk in response.body_iterator])
    assert seen == ["plan-1"] and "multiskill_result" in body and body.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_confirmed_multiskill_plan_executes_same_plan_without_planner():
    canonical = plan(step(task="confirmed task"))
    calls = []
    async def runtime(**kwargs):
        calls.append(kwargs)
        return {"success": True, "text": "ok"}
    result = await manager.run_multiskill_manager(user_request="original", parent_envelope={},
        activation_cards=[{"skill_name": "one"}], single_skill_runtime=runtime,
        confirmed_plan=canonical, model_call=lambda *_: pytest.fail("planner must not run"),
        final_synthesizer=lambda **_: "final")
    assert result["success"] is True
    assert calls[0]["input_envelope"]["user_request"] == "confirmed task"


def test_plan_preview_stays_at_skill_and_manifest_level():
    canonical = plans.validate_multiskill_plan(plan(step(), step("s2", "two", "present", {
        "payload": {"source_type": "skill_result", "step_id": "s1", "channel": "structured_outputs", "path": "summary"}
    }, ["s1"])), ["one", "two"])
    preview = manager.preview_multiskill_plan(canonical)
    assert preview[1]["input_sources"] == [{"target": "payload", "source": "s1.structured_outputs.summary"}]
    assert "command" not in str(preview) and "script" not in str(preview)
