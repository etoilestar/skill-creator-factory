import json

import pytest

from backend.services.creator.common import responsibility_binding_context_from_graph
from backend.services.creator.generation import _build_generate_file_prompt
from backend.services.creator.contracts import _review_skill_md_blueprint_intent_with_model


EDGE = {
    "from_node": "scripts/source.py",
    "from_output": "items",
    "to_node": "scripts/downstream.py",
    "to_input": "items",
    "purpose": "handoff",
    "constraints": [],
}


def test_writer_prompt_contains_non_empty_binding_context():
    context = responsibility_binding_context_from_graph({"dataflow_edges": [EDGE]})
    messages = _build_generate_file_prompt(
        "SKILL.md",
        "demo",
        "demo skill",
        "files: scripts/source.py scripts/downstream.py",
        [],
        responsibility_binding_context=context,
    )
    prompt = "\n".join(str(m.get("content", "")) for m in messages)
    assert '"scripts/downstream.py"' in prompt
    assert '"from_output": "items"' in prompt


@pytest.mark.asyncio
async def test_semantic_judge_receives_same_binding_context_and_reports_mismatch(monkeypatch):
    captured = {}

    def fake_route(*args, **kwargs):
        class Route:
            model = "test-model"
        return Route()

    async def fake_complete(messages, model):
        captured["prompt"] = messages[-1]["content"]
        assert "command_value_source_mismatch" in captured["prompt"]
        return json.dumps({
            "passed": False,
            "required_script_paths": ["scripts/downstream.py"],
            "reviewers": {},
            "issues": [{
                "severity": "error",
                "blocking": True,
                "field": "workflow",
                "category": "command_value_source_mismatch",
                "issue_type": "command_value_source_mismatch",
                "target_script": "scripts/downstream.py",
                "argv_key": "items",
                "current_value": "__RUNTIME_INPUT_FILE__",
                "from_node": "scripts/source.py",
                "from_output": "items",
                "contract_impact": {"execution_closure": True},
                "message": "value source mismatch",
                "expected": "{{items}}",
                "minimal_edit": "only patch argv.items",
            }],
            "repair_suggestions": "",
        })

    monkeypatch.setattr("backend.services.creator.contracts.route_model", fake_route)
    monkeypatch.setattr("backend.services.creator.contracts.complete_chat_once", fake_complete)
    monkeypatch.setattr("backend.services.creator.contracts._log_creator_model_usage", lambda **kwargs: None)

    content = """---\nname: demo\ndescription: demo\n---\n```bash\npython scripts/downstream.py '{"items":"__RUNTIME_INPUT_FILE__"}'\n```\n"""
    context = responsibility_binding_context_from_graph({"dataflow_edges": [EDGE]})
    review = await _review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content=content,
        blueprint_text="files: scripts/source.py scripts/downstream.py",
        skill_plan_entry=None,
        requirement_graph={"dataflow_edges": [EDGE]},
        responsibility_binding_context=context,
        model="test-model",
    )
    assert '"scripts/downstream.py"' in captured["prompt"]
    assert '"from_output": "items"' in captured["prompt"]
    assert review["passed"] is False
    assert review["issues"][0]["issue_type"] == "command_value_source_mismatch"


@pytest.mark.asyncio
async def test_semantic_judge_passed_true_is_not_overridden_by_backend(monkeypatch):
    def fake_route(*args, **kwargs):
        class Route:
            model = "test-model"
        return Route()

    async def fake_complete(messages, model):
        assert "command_value_source_mismatch" in messages[-1]["content"]
        return json.dumps({"passed": True, "required_script_paths": ["scripts/downstream.py"], "reviewers": {}, "issues": []})

    monkeypatch.setattr("backend.services.creator.contracts.route_model", fake_route)
    monkeypatch.setattr("backend.services.creator.contracts.complete_chat_once", fake_complete)
    monkeypatch.setattr("backend.services.creator.contracts._log_creator_model_usage", lambda **kwargs: None)

    content = """---
name: demo
description: demo
---
```bash
python scripts/downstream.py '{"items":"__RUNTIME_INPUT_FILE__"}'
```
"""
    context = responsibility_binding_context_from_graph({"dataflow_edges": [EDGE]})
    review = await _review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content=content,
        blueprint_text="files: scripts/source.py scripts/downstream.py",
        skill_plan_entry=None,
        requirement_graph={"dataflow_edges": [EDGE]},
        responsibility_binding_context=context,
        model="test-model",
    )
    assert review["passed"] is True
    assert review["issues"] == []


@pytest.mark.asyncio
async def test_semantic_judge_accepts_matching_from_output_placeholder(monkeypatch):
    def fake_route(*args, **kwargs):
        class Route:
            model = "test-model"
        return Route()

    async def fake_complete(messages, model):
        return json.dumps({"passed": True, "required_script_paths": ["scripts/downstream.py"], "reviewers": {}, "issues": []})

    monkeypatch.setattr("backend.services.creator.contracts.route_model", fake_route)
    monkeypatch.setattr("backend.services.creator.contracts.complete_chat_once", fake_complete)
    monkeypatch.setattr("backend.services.creator.contracts._log_creator_model_usage", lambda **kwargs: None)

    content = """---\nname: demo\ndescription: demo\n---\n```bash\npython scripts/downstream.py '{"items":"{{items}}"}'\n```\n"""
    context = responsibility_binding_context_from_graph({"dataflow_edges": [EDGE]})
    review = await _review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content=content,
        blueprint_text="files: scripts/source.py scripts/downstream.py",
        skill_plan_entry=None,
        requirement_graph={"dataflow_edges": [EDGE]},
        responsibility_binding_context=context,
        model="test-model",
    )
    assert review["passed"] is True
