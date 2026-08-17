import json
from pathlib import Path

import pytest

from backend.routers.sandbox import child_skill_runtime, multiskill_executor, multiskill_manager, multiskill_plan
from backend.routers.sandbox.adaptive_runtime import empty_adaptive_policy
from backend.routers.sandbox.runtime_execution_plan import VERSION as RUNTIME_VERSION


def _write_skill(root: Path, name: str, script: str, *, input_name: str, output_name: str):
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(f"""---
name: {name}
description: integration skill
---
role: transformer
inputs: [{input_name}]
outputs: [{output_name}]
```bash
python scripts/run.py '{{\"{input_name}\":\"{{{{{input_name}}}}}\"}}'
```
""", encoding="utf-8")
    (root / "scripts" / "run.py").write_text(script, encoding="utf-8")


def _runtime_plan(input_name, output_name, *, source="text"):
    return {"version": RUNTIME_VERSION, "steps": [{
        "step_id": "run", "script_path": "scripts/run.py", "description": "run skill",
        "bindings": {input_name: {"source_type": "envelope", "source": source}},
        "expected_outputs": [output_name],
    }]}


@pytest.fixture
def real_skills(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ("one", "two", "files", "failure")}
    _write_skill(roots["one"], "one",
        "import json,sys\np=json.loads(sys.argv[1]); print(json.dumps({'summary':'hello'}))\n",
        input_name="text", output_name="summary")
    _write_skill(roots["two"], "two",
        "import json,sys\np=json.loads(sys.argv[1]); print(json.dumps({'result':p['payload']}))\n",
        input_name="payload", output_name="result")
    _write_skill(roots["files"], "files",
        "import json,sys,pathlib\np=json.loads(sys.argv[1]); f=pathlib.Path(p['file']); print(json.dumps({'result':f.read_text()}))\n",
        input_name="file", output_name="result")
    _write_skill(roots["failure"], "failure",
        "import sys\nprint('failed', file=sys.stderr); raise SystemExit(7)\n",
        input_name="text", output_name="result")

    def resolve(name, **kwargs):
        assert kwargs == {"mode": "sandbox", "require_visible": True, "require_executable": True}
        return {"name": name, "root_path": str(roots[name])}
    for module in (child_skill_runtime, multiskill_executor, multiskill_plan):
        monkeypatch.setattr(module, "resolve_skill_record", resolve)
    monkeypatch.setattr("backend.services.skill_governance.allowed_skill_roots", lambda: [tmp_path])
    return roots


@pytest.mark.asyncio
async def test_real_child_runtime_executes_existing_single_skill_runtime(real_skills, monkeypatch):
    calls = []
    original_schema = child_skill_runtime._build_runtime_action_schema
    original_runtime = child_skill_runtime.execute_skill_workflow
    def schema(*args, **kwargs):
        calls.append("schema")
        return original_schema(*args, **kwargs)
    async def runtime(**kwargs):
        calls.append("runtime")
        return await original_runtime(**kwargs)
    monkeypatch.setattr(child_skill_runtime, "_build_runtime_action_schema", schema)
    monkeypatch.setattr(child_skill_runtime, "execute_skill_workflow", runtime)
    result = await child_skill_runtime.execute_child_skill_runtime(skill_name="one",
        input_envelope={"user_request": "analyze", "text": "input"}, child_run_id="real-1",
        dataflow_plan=_runtime_plan("text", "summary"))
    assert calls == ["schema", "runtime"]
    assert result["success"] is True
    assert result["structured_outputs"] == [{"source": "stdout", "data": {"summary": "hello"}}]


def _install_runtime_planners(monkeypatch, *, checkpoint=False):
    plans = {
        "one": _runtime_plan("text", "summary"),
        "two": _runtime_plan("payload", "result", source="payload"),
        "files": _runtime_plan("file", "result", source="input_files.0.path"),
        "failure": _runtime_plan("text", "result"),
    }
    async def planner(**kwargs): return plans[kwargs["skill_name"]]
    async def adaptive(**kwargs):
        policy = empty_adaptive_policy()
        if checkpoint:
            policy["checkpoints"] = [{"after_step_id": "run", "reason": "confirm"}]
        return policy
    monkeypatch.setattr("backend.routers.sandbox.workflow_dataflow._plan_workflow_steps_with_model", planner)
    monkeypatch.setattr("backend.routers.sandbox.workflow_dataflow._plan_adaptive_policy_with_model", adaptive)


def _two_skill_plan(second_bindings):
    return {"version": multiskill_plan.VERSION, "steps": [
        {"step_id": "s1", "skill_name": "one", "task": "analyze", "bindings": {}, "depends_on": []},
        {"step_id": "s2", "skill_name": "two", "task": "present", "bindings": second_bindings, "depends_on": ["s1"]},
    ]}


@pytest.mark.asyncio
async def test_real_multiskill_two_skill_chain(real_skills, monkeypatch):
    _install_runtime_planners(monkeypatch)
    result = await multiskill_executor.execute_multiskill_plan(plan=_two_skill_plan({
        "payload": {"source_type": "skill_result", "step_id": "s1", "channel": "structured_outputs", "path": "[0].data.summary"}
    }), activated_skill_names=["one", "two"], parent_envelope={"text": "input"},
        single_skill_runtime=None)
    assert result["success"] is True
    assert result["skill_results"]["s2"]["structured_outputs"][0]["data"]["result"] == "hello"


@pytest.mark.asyncio
async def test_real_result_manifest_binding(real_skills, monkeypatch):
    _install_runtime_planners(monkeypatch)
    result = await multiskill_executor.execute_multiskill_plan(plan=_two_skill_plan({
        "payload": {"source_type": "skill_result", "step_id": "s1", "channel": "structured_outputs", "path": "[0].data.summary"}
    }), activated_skill_names=["one", "two"], parent_envelope={"text": "input"},
        single_skill_runtime=None)
    assert result["skill_results"]["s1"]["structured_outputs"] == [
        {"source": "stdout", "data": {"summary": "hello"}}
    ]


@pytest.mark.asyncio
async def test_real_artifact_handoff(real_skills, monkeypatch):
    # Replace Skill A with an artifact-producing runtime package.
    (real_skills["one"] / "scripts" / "run.py").write_text(
        "import json,pathlib\np=pathlib.Path('outputs/result.txt'); p.parent.mkdir(exist_ok=True); p.write_text('artifact data'); print(json.dumps({'summary':'hello','output_file':'outputs/result.txt'}))\n",
        encoding="utf-8")
    # Planner calls are mocked, but the production adapter/runtime/executor are not.
    plan = _two_skill_plan({"input_files": {
        "source_type": "skill_result", "step_id": "s1", "channel": "output_files"}})
    # The second package consumes files rather than payload.
    plan["steps"][1]["skill_name"] = "files"
    _install_runtime_planners(monkeypatch)
    result = await multiskill_executor.execute_multiskill_plan(plan=plan,
        activated_skill_names=["one", "files"], parent_envelope={"text": "input"}, single_skill_runtime=None)
    assert result["success"] is True
    copied = next((real_skills["files"] / "inputs").rglob("*-result.txt"))
    # The bridge generated the canonical manifest consumed by the real file skill.
    assert result["skill_results"]["s2"]["structured_outputs"][0]["data"]["result"] == "artifact data"
    assert copied.read_text() == "artifact data"


@pytest.mark.asyncio
async def test_real_parent_uploaded_file_to_first_child(real_skills, monkeypatch, tmp_path):
    _install_runtime_planners(monkeypatch)
    upload_root = tmp_path / "platform-uploads"
    source = upload_root / "inputs" / "session" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_text("uploaded content", encoding="utf-8")
    plan = {"version": multiskill_plan.VERSION, "steps": [{
        "step_id": "s1", "skill_name": "files", "task": "read upload",
        "bindings": {"input_files": {"source_type": "envelope", "path": "input_files"}},
        "depends_on": [],
    }]}
    parent = {"text": "read", "_platform_input_root": str(upload_root), "input_files": [{
        "path": "inputs/session/test.txt", "filename": "test.txt", "size": 999,
        "mime_type": "text/plain",
    }]}
    result = await multiskill_executor.execute_multiskill_plan(plan=plan,
        activated_skill_names=["files"], parent_envelope=parent)
    assert result["skill_results"]["s1"]["structured_outputs"][0]["data"]["result"] == "uploaded content"
    copied = next((real_skills["files"] / "inputs").rglob("*-test.txt"))
    assert copied.read_text() == "uploaded content"
    assert str(source.resolve()) not in str(result)


def test_artifact_bridge_rejects_path_traversal(real_skills, tmp_path):
    outside = tmp_path / "secret.txt"; outside.write_text("secret")
    with pytest.raises(multiskill_executor.MultiSkillBindingError, match="confirmed"):
        multiskill_executor.materialize_child_artifacts_for_input(items=[{"path": "../secret.txt"}],
            source_skill_name="one", target_skill_name="two", child_run_id="safe")


@pytest.mark.asyncio
async def test_real_child_ask_user_pauses_parent(real_skills, monkeypatch):
    async def decide(**kwargs):
        return {"action": "ask_user", "reason": "need approval", "missing": ["approval"]}
    monkeypatch.setattr("backend.routers.sandbox.workflow_dataflow._decide_after_observation", decide)
    _install_runtime_planners(monkeypatch, checkpoint=True)
    result = await multiskill_executor.execute_multiskill_plan(plan=_two_skill_plan({
        "payload": {"source_type": "default", "value": "unused"}}), activated_skill_names=["one", "two"],
        parent_envelope={"text": "input"}, single_skill_runtime=None)
    assert result["success"] is None and result["completed"] is False
    assert result["paused_for_user"] is True and result["paused_at_step_id"] == "s1"
    assert result["missing"] == ["approval"]


@pytest.mark.asyncio
async def test_real_child_failure_stops_parent(real_skills, monkeypatch):
    _install_runtime_planners(monkeypatch)
    plan = _two_skill_plan({"payload": {"source_type": "default", "value": "unused"}})
    plan["steps"][0]["skill_name"] = "failure"
    result = await multiskill_executor.execute_multiskill_plan(plan=plan,
        activated_skill_names=["failure", "two"], parent_envelope={"text": "input"}, single_skill_runtime=None)
    assert result["success"] is False and result["failed_skill_step_id"] == "s1"
    assert "s2" not in result["skill_results"]


@pytest.mark.asyncio
async def test_real_single_skill_fast_path(real_skills, monkeypatch):
    _install_runtime_planners(monkeypatch)
    monkeypatch.setattr(multiskill_manager, "build_multiskill_catalog", lambda **_: [{"name": "one"}])
    async def shortlist(**kwargs): return {"candidates": [{"skill_name": "one"}]}
    monkeypatch.setattr(multiskill_manager, "_plan_skill_candidates_with_model", shortlist)
    monkeypatch.setattr(multiskill_manager, "build_multiskill_activation_cards", lambda _: [{"skill_name": "one"}])
    result = await multiskill_manager.run_multiskill_orchestration(user_request="analyze",
        parent_envelope={"user_request": "analyze", "text": "input"}, single_skill_runtime=None,
        planner_model_call=lambda *_: json.dumps({"mode": "single_skill", "skill_name": "one"}),
        final_synthesizer=lambda **_: "final answer")
    assert result["mode"] == "single_skill" and result["skill_results"]["skill_1"]["structured_outputs"][0]["data"]["summary"] == "hello"
    assert result["text"] == "final answer"
    assert result["child_run_id"].startswith("child_")


@pytest.mark.asyncio
async def test_real_single_skill_fast_path_with_uploaded_file(real_skills, monkeypatch, tmp_path):
    _install_runtime_planners(monkeypatch)
    upload_root = tmp_path / "pool-uploads"
    source = upload_root / "inputs" / "session" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_text("single upload", encoding="utf-8")
    monkeypatch.setattr(multiskill_manager, "build_multiskill_catalog", lambda **_: [{"name": "files"}])
    async def shortlist(**kwargs): return {"candidates": [{"skill_name": "files"}]}
    monkeypatch.setattr(multiskill_manager, "_plan_skill_candidates_with_model", shortlist)
    monkeypatch.setattr(multiskill_manager, "build_multiskill_activation_cards",
        lambda _: [{"skill_name": "files"}])
    parent = {
        "user_request": "read upload", "input": "read upload", "text": "read upload",
        "payload": None, "fields": {}, "options": {}, "resources": [],
        "input_files": [{"path": "inputs/session/test.txt", "filename": "test.txt"}],
        "files": [{"path": "inputs/session/test.txt", "filename": "test.txt"}],
        "_platform_input_root": str(upload_root),
    }
    result = await multiskill_manager.run_multiskill_orchestration(user_request="read upload",
        parent_envelope=parent,
        planner_model_call=lambda *_: json.dumps({"mode": "single_skill", "skill_name": "files"}),
        final_synthesizer=lambda **_: "read complete")
    child = result["skill_results"]["skill_1"]
    assert child["structured_outputs"][0]["data"]["result"] == "single upload"
    assert result["text"] == "read complete"
    assert next((real_skills["files"] / "inputs").rglob("*-test.txt")).read_text() == "single upload"
    assert str(upload_root.resolve()) not in str(result)


@pytest.mark.parametrize("location,payload", [
    ("top", {"parallel": True}), ("step", {"retry": 2}),
    ("binding", {"source_type": "default", "value": 1, "priority": 9}),
])
def test_unknown_plan_fields_are_rejected(real_skills, location, payload):
    base = {"version": multiskill_plan.VERSION, "steps": [{
        "step_id": "s1", "skill_name": "one", "task": "run", "bindings": {}, "depends_on": []}]}
    if location == "top": base.update(payload)
    elif location == "step": base["steps"][0].update(payload)
    else: base["steps"][0]["bindings"]["payload"] = payload
    with pytest.raises(multiskill_plan.MultiSkillPlanError):
        multiskill_plan.validate_multiskill_plan(base, ["one"])
