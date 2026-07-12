import inspect
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

from types import SimpleNamespace

from backend.services.creator.common import (
    ResponsibilityGraph,
    build_function_execution_context,
    validate_responsibility_graph_schema,
)
from backend.services.creator.generation import _script_local_contract_payload
from backend.services.creator.api import generate_file
from backend.services.creator import api as creator_api
from backend.services.creator import e2e as creator_e2e


def test_skill_md_generate_file_source_no_early_success_return_before_format_gate():
    import inspect
    source = inspect.getsource(generate_file)
    assert "skip SKILL.md static format/contract repair" not in source
    assert "skill_md_generation_failed" not in source
    assert source.index("_first_round_format_stage_error") < source.index("_validate_skill_md_blueprint_alignment")


def test_reference_semantic_review_helper_exists_and_uses_validator_task():
    import inspect
    source = inspect.getsource(repair._run_reference_semantic_review)
    assert "VALIDATOR_TASK" in source
    assert "token overlap" in source
    assert "passed" in source and "issues" in source and "repair_instructions" in source


def test_markdown_format_failures_use_full_rewrite_not_region_rewrite_in_main_path():
    import inspect
    source = inspect.getsource(generate_file)
    marker = 'prompt_variant="rewrite_markdown_full_format"'
    start = source.rindex("if is_markdown_hard_format_error(stage_error) and _is_markdown_creator_file(request.file_path):", 0, source.index(marker))
    end = source.index("continue", source.index(marker))
    block = source[start:end]
    assert "format_full_rewrite" in block
    assert "_build_markdown_format_full_rewrite_prompt" in block
    assert "format_region_rewrite" not in block
    assert "_build_markdown_region_rewrite_prompt" not in block


def test_python_compile_failures_use_full_rewrite_prompt_not_semantic_patch():
    import inspect
    source = inspect.getsource(generate_file)
    marker = "is_compile_rewrite_error ="
    block = source[source.index(marker):source.index('if error_source == "model_empty_content"', source.index(marker))]
    assert "_build_strict_compile_rewrite_prompt" in block
    assert "strict_compile_rewrite" in block
    assert "repair_mode" not in block


def test_semantic_patch_path_continues_to_next_loop_for_format_then_semantic():
    import inspect
    source = inspect.getsource(generate_file)
    repair_call = source.index("repaired_candidate = await _repair_generated_file_with_feedback")
    next_continue = source.index("continue", repair_call)
    next_format_gate = source.index("_first_round_format_stage_error")
    next_semantic_gate = source.index("_run_script_responsibility_review")
    assert next_continue > repair_call
    assert next_format_gate < next_semantic_gate


def test_writer_and_judge_use_json_equivalent_function_execution_context(monkeypatch):
    entry = _entry()
    binding = {"primary_tool_ids": ["script_argv_guard"]}
    graph = {"function_items": [_req().model_dump()], "responsibility_edges": []}

    writer_payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose=entry.purpose,
        plan_entry=entry,
        stdout_schema={"type": "object"},
        requirements=[_req()],
        responsibility_graph=graph,
    )
    writer_context = writer_payload["function_execution_context"]
    judge_context = build_function_execution_context(
        graph=graph,
        target_file="scripts/main.py",
        current_file_tool_binding=writer_payload["current_file_tool_binding"],
        fallback_function_item=_req(),
    )

    assert writer_context["function_item"] == judge_context["function_item"]
    assert writer_context["incoming_edges"] == judge_context["incoming_edges"]
    assert writer_context["outgoing_edges"] == judge_context["outgoing_edges"]
    assert writer_context["authorized_tool_contracts"] == judge_context["authorized_tool_contracts"]


def test_responsibility_graph_rejects_non_python_scripts_and_accepts_python():
    files = [SimpleNamespace(path="scripts/main.py", purpose="do x")]
    graph = ResponsibilityGraph(requirements=[_req()])
    assert validate_responsibility_graph_schema(graph, files).function_items

    for bad in ("scripts/main.js", "scripts/run.sh"):
        bad_graph = ResponsibilityGraph(requirements=[RequirementItem(target_file=bad, purpose="bad")])
        try:
            validate_responsibility_graph_schema(bad_graph, [SimpleNamespace(path=bad, purpose="bad")])
        except Exception as exc:
            assert "scripts/**/*.py" in str(exc)
        else:
            raise AssertionError(f"{bad} should not be a FunctionItem target")


def test_scriptless_responsibility_graph_still_valid():
    graph = ResponsibilityGraph(requirements=[])
    assert validate_responsibility_graph_schema(graph, [SimpleNamespace(path="SKILL.md", purpose="docs")]).function_items == []


def test_e2e_source_does_not_call_semantic_judge_or_toolpool_planning():
    import inspect
    source = inspect.getsource(creator_e2e)
    forbidden = [
        "_run_script_responsibility_review(",
        "_run_reference_semantic_review(",
        "_plan_tool_pool_patch_from_responsibility_feedback(",
        "explore_tool_pool(",
        "rediscover_for_repair=True",
    ]
    for token in forbidden:
        assert token not in source

from backend.services.skill_plan import normalize_structured_function_items


def _structured_function_item(path):
    return {
        "target_file": path,
        "role": "generic_script",
        "purpose": "do work",
        "inputs": [],
        "outputs": [],
        "required_capabilities": [],
        "constraints": [],
    }

def test_normalize_structured_function_items_is_python_only():
    assert normalize_structured_function_items([_structured_function_item("scripts/main.py")])[0]["target_file"] == "scripts/main.py"
    for bad in ("scripts/main.js", "scripts/run.sh", "references/ref.md"):
        try:
            normalize_structured_function_items([_structured_function_item(bad)])
        except ValueError as exc:
            assert "must_be_scripts_python" in str(exc)
        else:
            raise AssertionError(f"{bad} should not normalize as a FunctionItem")



def test_writer_and_judge_reuse_provided_context_without_rebuilding(monkeypatch):
    entry = _entry()
    shared_context = {
        "function_item": {"target_file": "scripts/main.py", "purpose": "shared"},
        "incoming_edges": [],
        "outgoing_edges": [],
        "authorized_tool_contracts": [],
    }
    build_calls = {"writer": 0, "judge": 0}

    def fail_writer_build(*args, **kwargs):
        build_calls["writer"] += 1
        raise AssertionError("writer should reuse provided function_execution_context")

    def fail_judge_build(*args, **kwargs):
        build_calls["judge"] += 1
        raise AssertionError("judge should reuse provided function_execution_context")

    monkeypatch.setattr("backend.services.creator.generation.build_function_execution_context", fail_writer_build)
    monkeypatch.setattr(repair, "build_function_execution_context", fail_judge_build)

    writer_payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose=entry.purpose,
        plan_entry=entry,
        stdout_schema={"type": "object"},
        requirements=[_req()],
        responsibility_graph={"function_items": [_req().model_dump()], "responsibility_edges": []},
        function_execution_context=shared_context,
    )
    assert writer_payload["function_execution_context"] == shared_context

    async def fake_complete(messages, model):
        assert "shared" in messages[-1]["content"]
        return json.dumps({"passed": True, "blocking_issues": [], "advisory_notes": []})

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    import asyncio
    result = asyncio.run(repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=entry,
        requirements=[],
        review_context={"function_execution_context": shared_context},
    ))
    assert result["passed"] is True
    assert build_calls == {"writer": 0, "judge": 0}


def test_judge_rebuilds_context_when_toolpool_context_not_provided(monkeypatch):
    calls = {"judge": 0}
    rebuilt_context = {
        "function_item": {"target_file": "scripts/main.py", "purpose": "rebuilt_after_toolpool_patch"},
        "incoming_edges": [],
        "outgoing_edges": [],
        "authorized_tool_contracts": [{"tool_id": "new.tool", "functions": []}],
    }

    def fake_build(*args, **kwargs):
        calls["judge"] += 1
        return rebuilt_context

    async def fake_complete(messages, model):
        assert "rebuilt_after_toolpool_patch" in messages[-1]["content"]
        assert "new.tool" in messages[-1]["content"]
        return json.dumps({"passed": True, "blocking_issues": [], "advisory_notes": []})

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "build_function_execution_context", fake_build)
    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    import asyncio
    result = asyncio.run(repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=_entry(),
        requirements=[],
        review_context={"current_file_tool_binding": {"primary_tool_ids": ["new.tool"]}},
    ))
    assert result["passed"] is True
    assert calls["judge"] == 1


def test_is_python_function_item_target_single_implementation_source():
    import inspect
    import backend.services.skill_plan as skill_plan_module
    import backend.services.creator.common as common_module

    assert common_module.is_python_function_item_target is skill_plan_module.is_python_function_item_target
    assert inspect.getsourcefile(common_module.is_python_function_item_target) == inspect.getsourcefile(skill_plan_module.is_python_function_item_target)


def test_initial_function_execution_context_prefers_real_toolpool_binding():
    import inspect
    source = inspect.getsource(generate_file)
    helper_start = source.index("def _build_current_function_execution_context")
    helper_end = source.index("function_execution_context: dict[str, Any] | None = _build_current_function_execution_context()", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "load_tool_pool(settings.skills_path / skill_name)" in helper_source
    assert "get_file_binding(context_tool_pool, request.file_path)" in helper_source
    assert helper_source.index("load_tool_pool(settings.skills_path / skill_name)") < helper_source.index("runtime_contract")
    assert helper_source.index("get_file_binding(context_tool_pool, request.file_path)") < helper_source.index("effective_skill_plan_entry.get(\"tool_binding_summary\")")


def test_toolpool_augmentation_rebuilds_function_execution_context_immediately():
    import inspect
    source = inspect.getsource(generate_file)
    marker = "tool_re_explore_count += 1"
    start = source.index(marker)
    block = source[start:source.index("except Exception as planning_exc", start)]
    assert "function_execution_context = _build_current_function_execution_context()" in block
    assert "function_execution_context = None" not in block


@pytest.mark.asyncio
async def test_repair_model_uses_new_canonical_context_instead_of_old_writer_tool_context(monkeypatch):
    captured = {}

    async def fake_request_and_apply(**kwargs):
        captured["task_context"] = kwargs["task_context"]
        return ({}, kwargs["current_content"].replace("old_call", "new_call"), {"changed_lines": 1})

    monkeypatch.setattr(repair, "_request_and_apply_repair_patch", fake_request_and_apply)
    monkeypatch.setattr(repair, "_apply_deterministic_micro_patch_if_safe", lambda **kwargs: None)
    monkeypatch.setattr(repair, "_creator_tool_context_for_script", lambda **kwargs: "fresh_tool_context")
    monkeypatch.setattr(repair, "resolve_tool_snippets_for_context", lambda **kwargs: [])
    monkeypatch.setattr(repair, "tool_snippet_prompt", lambda snippets: "")

    old_prompt_messages = [{"role": "user", "content": "old_writer_tool_context should not leak"}]
    new_context = {
        "function_item": {"target_file": "scripts/main.py", "purpose": "new canonical"},
        "incoming_edges": [],
        "outgoing_edges": [],
        "authorized_tool_contracts": [{"tool_id": "fresh.tool", "functions": []}],
    }

    result = await repair._repair_generated_file_with_feedback(
        prompt_messages=old_prompt_messages,
        model="unit-test-model",
        file_path="scripts/main.py",
        previous_content="def run():\n    old_call()\n",
        validation_error="script_requirement_failed",
        targeted_repair="use new tool",
        skill_plan_entry={"path": "scripts/main.py", "role": "generic_script"},
        current_file_binding={"primary_tool_ids": ["fresh.tool"]},
        tool_pool_summary={"bindings": ["fresh.tool"]},
        function_execution_context=new_context,
    )

    assert "new_call" in result
    assert "fresh.tool" in captured["task_context"]
    assert "new canonical" in captured["task_context"]
    assert "old_writer_tool_context" not in captured["task_context"]

from backend.services.creator import contracts, generation
from backend.services.creator.contracts import ContractValidationError


def test_skill_md_generation_prompt_contains_dataflow_alignment_context(monkeypatch, tmp_path):
    monkeypatch.setattr(generation.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "generic"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "alpha.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def run(argv):\n"
        "    strict_json_argv_guard(argv, {'opaque_in': {'required': True, 'type': 'string'}})\n"
        "    return {'opaque_out': argv['opaque_in']}\n",
        encoding="utf-8",
    )
    graph = {"requirements": [{"target_file": "scripts/alpha.py", "outputs": ["opaque_out"]}], "dataflow_edges": [
        {"from_node": "platform_input", "from_output": "opaque_seed", "to_node": "scripts/alpha.py", "to_input": "opaque_in", "purpose": "transport", "constraints": ["none"]}
    ]}

    messages = generation._build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="generic",
        purpose="generic",
        blueprint_text="files: scripts/alpha.py",
        conversation_history=[],
        responsibility_graph=graph,
    )

    prompt = "\n".join(message["content"] for message in messages)
    assert "统一 SKILL.md dataflow alignment context" in prompt
    assert "dataflow_edges" in prompt
    assert "strict_json_argv_guard_probe" in prompt
    assert "declared_stdout_fields" in prompt
    assert "runtime_stdout_protocol" in prompt
    assert "不得根据 key 名相似度" in prompt


@pytest.mark.asyncio
async def test_skill_md_dataflow_validator_reports_model_alignment_issue(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts.settings, "skills_path", tmp_path)

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(contracts, "_log_creator_model_usage", lambda **kwargs: None)

    async def fake_complete(messages, model):
        prompt = messages[-1]["content"]
        assert "dataflow_edges" in prompt
        assert "current_skill_md_commands" in prompt
        return json.dumps({
            "passed": False,
            "issues": [{
                "code": "skill_md_command_value_misaligned",
                "script_path": "scripts/b.py",
                "argv_key": "opaque_in",
                "current_value": "{{wrong}}",
                "source_edge": {"from_node": "node_a", "from_output": "opaque_out", "to_node": "scripts/b.py", "to_input": "opaque_in"},
                "reason": "validator model found graph/probe mismatch",
                "repair_scope": "command_argv_value_only",
                "repair_instruction": "only replace argv value for opaque_in",
                "evidence": "edge + argv/stdout probe",
            }],
            "repair_suggestions": "repair value only",
        })

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)

    with pytest.raises(ContractValidationError) as exc:
        await contracts._validate_skill_md_dataflow_alignment(
            skill_name="generic",
            content="```bash\npython scripts/b.py '{\"opaque_in\":\"{{wrong}}\"}'\n```\n",
            blueprint_text="files: scripts/b.py",
            requirement_graph={"dataflow_edges": [{"from_node": "node_a", "from_output": "opaque_out", "to_node": "scripts/b.py", "to_input": "opaque_in"}]},
            model="unit-test-model",
        )

    result = exc.value.results[0]
    assert result.id == "skill_md_command_value_misaligned"
    assert result.details["repair_scope"] == "command_argv_value_only"
    assert "后端不得直接重写 argv value" in str(exc.value)


@pytest.mark.asyncio
async def test_skill_md_dataflow_validation_can_be_called_again_after_repair(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts.settings, "skills_path", tmp_path)

    class Route:
        model = "unit-test-model"

    calls = {"count": 0}
    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(contracts, "_log_creator_model_usage", lambda **kwargs: None)

    async def fake_complete(messages, model):
        calls["count"] += 1
        if calls["count"] == 1:
            return json.dumps({"passed": False, "issues": [{
                "code": "skill_md_command_value_unresolved",
                "script_path": "scripts/r.py",
                "argv_key": "k",
                "current_value": "{{unknown}}",
                "source_edge": {},
                "reason": "insufficient evidence",
                "repair_scope": "command_argv_value_only",
                "repair_instruction": "do not guess; repair with evidence",
                "evidence": "missing stdout probe",
            }]})
        return json.dumps({"passed": True, "issues": [], "repair_suggestions": ""})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    kwargs = dict(skill_name="generic", content="```bash\npython scripts/r.py '{\"k\":\"{{unknown}}\"}'\n```\n", blueprint_text="scripts/r.py", requirement_graph={}, model="unit-test-model")
    with pytest.raises(ContractValidationError):
        await contracts._validate_skill_md_dataflow_alignment(**kwargs)
    passed = await contracts._validate_skill_md_dataflow_alignment(**kwargs)
    assert passed["passed"] is True
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_skill_md_value_repair_prompt_carries_value_only_constraints(monkeypatch):
    captured = {}

    async def fake_request_and_apply_repair_patch(**kwargs):
        captured.update(kwargs)
        return None, kwargs["current_content"], {"changed_line_count": 0, "applied": []}

    monkeypatch.setattr(repair, "_request_and_apply_repair_patch", fake_request_and_apply_repair_patch)

    content = "---\nname: S\ndescription: D\n---\n```bash\npython scripts/r.py '{\"k\":\"{{bad}}\"}'\n```\n"
    await repair._repair_generated_file_with_feedback(
        prompt_messages=[{"role": "user", "content": "unified dataflow alignment context with dataflow_edges and strict_json_argv_guard_probe"}],
        model="unit-test-model",
        file_path="SKILL.md",
        previous_content=content,
        validation_error="skill_md_command_value_misaligned",
        failed_checks_text=json.dumps({"failures": [{"id": "skill_md_command_value_misaligned", "script_path": "scripts/r.py", "argv_key": "k"}]}),
    )

    assert "只允许修改指定 command JSON argv 中指定 key 的 value" in captured["target_rule"]
    assert "不得修改 argv key" in captured["target_rule"]
    assert "完整 responsibility_graph/dataflow_edges" in captured["task_context"]
    assert "validator issue" in captured["task_context"]


def test_skill_md_dataflow_context_preserves_platform_nodes_and_probe_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "generic"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "probe.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def run(argv):\n"
        "    strict_json_argv_guard(argv, {'opaque_in': {'required': True}})\n"
        "    return {'static_out': argv['opaque_in']}\n",
        encoding="utf-8",
    )

    context = contracts._build_skill_md_dataflow_alignment_context(
        skill_name="generic",
        skill_md="```bash\npython scripts/probe.py '{\"opaque_in\":\"{{input}}\"}'\n```\n",
        blueprint_text="files: scripts/probe.py",
        requirement_graph={
            "platform_input_node": {"id": "pin", "fields": {"opaque_seed": "string"}},
            "platform_output_node": {"id": "pout", "outputs": ["final"]},
            "dataflow_edges": [{"from_node": "pin", "from_output": "opaque_seed", "to_node": "scripts/probe.py", "to_input": "opaque_in"}],
        },
    )

    graph = context["responsibility_graph"]
    assert graph["platform_input_node"] == {"id": "pin", "fields": {"opaque_seed": "string"}}
    assert graph["platform_output_node"] == {"id": "pout", "outputs": ["final"]}
    script = context["scripts"][0]
    assert script["declared_stdout_fields"] == []
    assert script["probed_stdout_fields"] == ["static_out"]
    assert script["actual_stdout_fields"] == []
    assert script["stdout_field_evidence"]["declared_stdout_fields_source"] == "SkillPlanEntry.outputs declared contract"
    assert script["stdout_field_evidence"]["actual_stdout_fields_source"] == "not executed in this context"
    protocol = context["platform_boundaries"]["runtime_stdout_protocol"]
    assert "payload.update(stdout_json)" in protocol
    assert "nested" not in protocol
    assert "from_node" not in context["platform_boundaries"]["placeholder_syntax"]


@pytest.mark.asyncio
async def test_skill_md_dataflow_validator_retries_empty_false_issues_then_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts.settings, "skills_path", tmp_path)

    class Route:
        model = "unit-test-model"

    calls = {"count": 0}
    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(contracts, "_log_creator_model_usage", lambda **kwargs: None)

    async def fake_complete(messages, model):
        calls["count"] += 1
        assert "passed=false requires" in messages[-1]["content"] or calls["count"] == 1
        return json.dumps({"passed": False, "issues": []})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)

    with pytest.raises(contracts.CreatorValidatorReviewError):
        await contracts._validate_skill_md_dataflow_alignment(
            skill_name="generic",
            content="```bash\npython scripts/a.py '{}'\n```\n",
            blueprint_text="scripts/a.py",
            requirement_graph={},
            model="unit-test-model",
        )
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_skill_md_value_alignment_repair_closed_loop_revalidates_and_preserves_scope(monkeypatch):
    class Route:
        model = "unit-test-model"

    validator_calls = {"count": 0}
    repair_calls = {"count": 0}
    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(contracts, "_log_creator_model_usage", lambda **kwargs: None)

    async def fake_complete(messages, model):
        validator_calls["count"] += 1
        if validator_calls["count"] == 1:
            return json.dumps({
                "passed": False,
                "issues": [{
                    "code": "skill_md_command_value_misaligned",
                    "script_path": "scripts/second.py",
                    "argv_key": "opaque_in",
                    "current_value": "{{bad_value}}",
                    "source_edge": {"from_node": "up", "from_output": "opaque_out", "to_node": "scripts/second.py", "to_input": "opaque_in"},
                    "reason": "value is not supported by graph and probe evidence",
                    "repair_scope": "command_argv_value_only",
                    "repair_instruction": "replace only opaque_in value in scripts/second.py command",
                    "evidence": "edge plus command argv summary",
                }],
            })
        return json.dumps({"passed": True, "issues": [], "repair_suggestions": ""})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)

    original = """---
name: S
description: D
---
Intro text stays.
```bash
python scripts/first.py '{"seed":"{{input}}"}'
```
```bash
python scripts/second.py '{"opaque_in":"{{bad_value}}","mode":"keep"}'
```
Outro text stays.
"""
    repaired_expected = original.replace('"opaque_in":"{{bad_value}}"', '"opaque_in":"{{opaque_out}}"')

    async def fake_request_and_apply_repair_patch(**kwargs):
        repair_calls["count"] += 1
        assert "只允许修改指定 command JSON argv 中指定 key 的 value" in kwargs["target_rule"]
        return None, repaired_expected, {"changed_line_count": 1, "applied": [{"fallback_type": "none"}]}

    monkeypatch.setattr(repair, "_request_and_apply_repair_patch", fake_request_and_apply_repair_patch)

    validate_kwargs = dict(
        skill_name="generic",
        blueprint_text="scripts/first.py scripts/second.py",
        requirement_graph={"dataflow_edges": [{"from_node": "up", "from_output": "opaque_out", "to_node": "scripts/second.py", "to_input": "opaque_in"}]},
        model="unit-test-model",
    )
    with pytest.raises(ContractValidationError) as exc:
        await contracts._validate_skill_md_dataflow_alignment(content=original, **validate_kwargs)

    repaired = await repair._repair_generated_file_with_feedback(
        prompt_messages=[{"role": "user", "content": "unified dataflow alignment context with current command block, edge, script probe, platform protocol"}],
        model="unit-test-model",
        file_path="SKILL.md",
        previous_content=original,
        validation_error=str(exc.value),
        failed_checks_text=json.dumps({"failures": [result.details for result in exc.value.results]}),
    )

    second = await contracts._validate_skill_md_dataflow_alignment(content=repaired, **validate_kwargs)
    assert second["passed"] is True
    assert validator_calls["count"] == 2
    assert repair_calls["count"] == 1
    assert repaired == repaired_expected
    assert "name: S" in repaired and "description: D" in repaired
    assert "Intro text stays." in repaired and "Outro text stays." in repaired
    assert "python scripts/first.py" in repaired
    assert "python scripts/second.py" in repaired
    assert '"opaque_in"' in repaired and '"mode":"keep"' in repaired
    assert "{{bad_value}}" not in repaired


def test_skill_md_generation_rewrite_and_repair_share_command_protocol_prompt(monkeypatch, tmp_path):
    from backend.services.creator.common import skill_md_command_protocol_text

    protocol = skill_md_command_protocol_text()
    monkeypatch.setattr(generation.settings, "skills_path", tmp_path)
    generation_messages = generation._build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="generic",
        purpose="generic",
        blueprint_text="files: scripts/run.py",
        conversation_history=[],
    )
    generation_prompt = "\n".join(message["content"] for message in generation_messages)

    rewrite_messages = creator_api._build_markdown_format_full_rewrite_prompt(
        file_path="SKILL.md",
        skill_name="generic",
        blueprint_text="files: scripts/run.py",
        deterministic_error="fence error",
        current_content="---\nname: S\ndescription: D\n---\n",
    )
    rewrite_prompt = "\n".join(message["content"] for message in rewrite_messages)

    repair_source = inspect.getsource(repair._repair_generated_file_with_feedback)

    assert protocol in generation_prompt
    assert protocol in rewrite_prompt
    assert "skill_md_command_protocol_text()" in repair_source
    assert "脚本路径后只允许一个参数" in protocol
    assert "经过 shell quoting 的 JSON object argv" in protocol
    assert "动态 {{placeholder}} 必须作为完整 JSON 字符串值" in protocol
