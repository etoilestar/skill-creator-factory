import json

import pytest

from backend.services.creator.contracts import _check_reference_file_contract
from backend.services.creator.common import RequirementItem
from backend.services.creator import repair
from backend.services.skill_plan import SkillPlanEntry


def _reference(text: str) -> str:
    return "---\ntitle: Tiny\ndescription: Short legal reference.\n---\n" + text


def _entry():
    return SkillPlanEntry(
        path="scripts/main.py",
        role="generic_script",
        file_type="script",
        purpose="Transform the payload into the required result.",
        runtime="python",
        language="python",
        inputs=["payload"],
        outputs=["result"],
        dependencies=[],
        required_capabilities=[],
    )


def _req():
    return RequirementItem(
        target_file="scripts/main.py",
        role="generic_script",
        runtime="python",
        purpose="Transform the payload into the required result.",
        inputs=["payload"],
        outputs=["result"],
        must_do=["Return a result derived from the payload."],
    )


def _failed_ids(results):
    return {r.id for r in results if not r.passed}


def test_reference_backend_does_not_require_purpose_body_token_overlap():
    results = _check_reference_file_contract(
        "references/style.md",
        _reference("# Alpha\n\nCompletely valid body text about layout."),
        purpose="financial risk calculation thresholds",
    )

    assert "reference.content.covers_own_semantic_purpose" not in _failed_ids(results)
    assert "reference.content.has_reference_value" not in _failed_ids(results)
    assert not _failed_ids(results)


def test_short_legal_reference_backend_passes_without_length_or_heading_gate():
    results = _check_reference_file_contract(
        "references/tiny.md",
        _reference("ok"),
        purpose="Long business purpose unrelated to body tokens.",
    )

    assert not _failed_ids(results)


@pytest.mark.parametrize(
    "content,expected_failed",
    [
        ("---\ntitle: Bad\ndescription: Bad\n---\n", {"markdown.body.missing", "reference.metadata.body_exists", "reference.not_empty"}),
        ("---\ntitle: [\ndescription: Bad\n---\nbody", {"markdown.frontmatter.invalid_yaml", "markdown.frontmatter.yaml_parse_failed"}),
        (_reference("```python\nprint('unterminated')\n"), {"markdown.fences.unclosed", "reference.markdown.fences_balanced"}),
    ],
)
def test_reference_backend_fails_objective_markdown_format(content, expected_failed):
    results = _check_reference_file_contract("references/bad.md", content, purpose="anything")

    assert expected_failed & _failed_ids(results)


@pytest.mark.asyncio
async def test_python_pass_stub_reaches_model_judge_instead_of_static_responsibility_failure(monkeypatch):
    calls = {"judge": 0}

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        calls["judge"] += 1
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "issue_type": "semantic_action_incomplete",
                "semantic_failure": "pass body does not implement the FunctionItem",
                "problem": "empty implementation",
                "minimal_edit": "implement the function",
            }],
            "repair_instructions": "implement the function",
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    pass\n",
        skill_plan_entry=_entry(),
        requirements=[],
        review_context={},
    )

    assert calls["judge"] == 1
    assert result["passed"] is False
    assert result.get("model") == "unit-test-model"


@pytest.mark.asyncio
async def test_python_missing_generic_variable_names_does_not_static_fail_before_judge(monkeypatch):
    calls = {"judge": 0}

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        calls["judge"] += 1
        prompt = messages[-1]["content"]
        assert "current script FunctionItem" in prompt
        return json.dumps({"passed": True, "blocking_issues": [], "advisory_notes": []})

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(x):\n    return {'result': x}\n",
        skill_plan_entry=_entry(),
        requirements=[],
        review_context={},
    )

    assert calls["judge"] == 1
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_writer_judge_shared_function_item_and_authorized_tool_contracts(monkeypatch):
    captured = {}

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        captured.setdefault("prompts", []).append(messages[-1]["content"])
        rid = _req().id
        return json.dumps({
            "passed": True,
            "checks": [{"requirement_id": rid, "passed": True}],
            "blocking_issues": [],
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)
    binding = {"primary_tool_ids": ["demo.tool"], "functions": [{"function_name": "demo"}]}

    await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=_entry(),
        requirements=[_req()],
        review_context={
            "requirement_graph": {"function_items": [_req().model_dump()], "responsibility_edges": []},
            "current_file_tool_binding": binding,
        },
    )

    prompt = captured["prompts"][0]
    assert "当前文件 FunctionItem graph context" in prompt
    assert "当前文件已授权工具合同" in prompt
    assert "scripts/main.py" in prompt
