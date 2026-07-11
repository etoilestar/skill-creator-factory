"""Focused regression tests for Creator final ToolPool planning."""

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.creator import api
from backend.services.creator.tool_pool_models import ToolPoolModel, ToolPoolTool
from backend.services.creator.tool_pool_store import load_tool_pool, save_tool_pool


def test_system_image_generation_is_selectable_with_existing_function_contract():
    catalog = api._creator_tool_catalog_for_planner()
    by_id = {tool["tool_id"]: tool for tool in catalog}

    assert "system_image_generation" in by_id
    functions = by_id["system_image_generation"]["functions"]
    assert any(
        function.get("function_name") == "generate_stable_diffusion_image"
        and function.get("import_path") == "backend.services.skill_runtime"
        for function in functions
    )


def test_creator_selectable_catalog_filters_capability_shells(monkeypatch):
    callable_tool = {
        "functions": [
            {
                "function_name": "call_me",
                "import_path": "backend.services.skill_runtime",
            }
        ]
    }
    no_functions = {"functions": []}
    missing_name = {
        "functions": [
            {
                "function_name": "",
                "import_path": "backend.services.skill_runtime",
            }
        ]
    }
    missing_import_path = {
        "functions": [
            {
                "function_name": "call_me",
                "import_path": "",
            }
        ]
    }

    assert api._creator_tool_has_callable_contract(callable_tool)
    assert not api._creator_tool_has_callable_contract(no_functions)
    assert not api._creator_tool_has_callable_contract(missing_name)
    assert not api._creator_tool_has_callable_contract(missing_import_path)


def test_embedding_recall_keeps_exact_match_plus_top_k_union_contract():
    source = inspect.getsource(api._recall_creator_tool_candidates)

    assert "exact Registry capability matches" in source
    assert "UNION" in source
    assert "per-query embedding top-k" in source


@pytest.mark.asyncio
async def test_recalled_exact_and_embedding_candidates_all_enter_desired_tool_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(
        api,
        "_recall_creator_tool_candidates",
        lambda file_specs, top_k: (
            [
                {"tool_id": "exact_alpha", "recalled_for_capabilities": ["exact"]},
                {"tool_id": "embedding_beta", "recalled_for_capabilities": ["embedding"]},
            ],
            "exact capability recall + embedding top-k recall union",
        ),
    )
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fail_if_called(**kwargs):
        raise AssertionError(f"unexpected LLM selector call: {kwargs.get('phase')}")

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fail_if_called)

    result = await api._plan_final_tool_pool(
        skill_name="demo",
        file_specs=[{"path": "scripts/a.py", "required": True, "required_capabilities": ["exact"]}],
    )

    assert result["desired_tool_ids"] == ["embedding_beta", "exact_alpha"]
    assert result["planner_output"]["desired_tool_ids"] == ["embedding_beta", "exact_alpha"]
    assert result["planner_output"]["candidate_tool_ids"] == ["exact_alpha", "embedding_beta"]
    assert result["planner_output"]["decisions"] == {"exact_alpha": True, "embedding_beta": True}
    assert result["planner_output"]["recall_source"] == "exact capability recall + embedding top-k recall union"
    assert result["planner_output"]["selection_mode"] == "recall_union_no_llm"


@pytest.mark.asyncio
async def test_final_tool_pool_planning_never_calls_llm_selector_phases(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id": "callable_alpha"}], "test"))
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fake_json(**kwargs):
        calls.append(kwargs.get("phase"))
        return {"decisions": {"callable_alpha": False}}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_json)

    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path": "scripts/a.py", "required": True}])

    assert calls == []
    assert "final_tool_selection" not in calls
    assert "final_tool_selection_convergence" not in calls
    assert result["desired_tool_ids"] == ["callable_alpha"]


@pytest.mark.asyncio
async def test_auto_final_tool_planning_is_add_only_and_disables_removal(monkeypatch, tmp_path):
    apply_kwargs = {}
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    save_tool_pool(
        skill_dir,
        ToolPoolModel(
            tools=[
                ToolPoolTool(tool_id="system_text_generation", status="allowed", source="system_required"),
                ToolPoolTool(tool_id="system_image_generation", status="allowed", source="system_required"),
                ToolPoolTool(tool_id="older_recalled_tool", status="allowed", source="blueprint_preselect"),
            ]
        ),
    )
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id": "new_recalled_tool"}], "test"))

    def fake_apply(**kwargs):
        apply_kwargs.update(kwargs)
        return {"patch_present": True}

    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", fake_apply)

    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path": "scripts/a.py", "required": True}])

    patch = result["computed_patch"]["tool_pool_patch"]
    assert patch["remove_tool_requests"] == []
    assert apply_kwargs["allow_remove"] is False
    assert result["desired_tool_ids"] == ["new_recalled_tool"]


@pytest.mark.asyncio
async def test_existing_system_generation_tools_are_not_deleted_when_not_recalled(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    save_tool_pool(
        skill_dir,
        ToolPoolModel(
            tools=[
                ToolPoolTool(tool_id="system_text_generation", status="allowed", source="system_required"),
                ToolPoolTool(tool_id="system_image_generation", status="allowed", source="system_required"),
            ]
        ),
    )
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([], "test"))

    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path": "scripts/a.py", "required": True}])

    pool = load_tool_pool(skill_dir)
    allowed = {tool.tool_id for tool in pool.tools if tool.status == "allowed"}
    assert result["computed_patch"]["tool_pool_patch"]["remove_tool_requests"] == []
    assert result["apply_result"]["removed"] == 0
    assert "system_text_generation" in allowed
    assert "system_image_generation" in allowed


def test_backend_gate_still_rejects_unusable_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    result = api._apply_planner_tool_pool_patch(
        skill_name="demo",
        planner_output={
            "tool_pool_patch": {
                "add_tool_requests": [
                    {"candidate_tool_id": "definitely_not_registered_tool"},
                    {"candidate_tool_id": "system_text_generation"},
                ],
                "remove_tool_requests": [],
            }
        },
        source_phase="final_contract_tool_planning",
        allow_remove=False,
    )

    pool = load_tool_pool(tmp_path / "demo")
    denied_ids = {item.tool_id for item in pool.denied_requests}
    allowed_ids = {tool.tool_id for tool in pool.tools if tool.status == "allowed"}
    assert "definitely_not_registered_tool" in denied_ids
    assert "system_text_generation" in allowed_ids
    assert result["denied_new"] >= 1


def test_final_tool_pool_computed_patch_uses_recall_union_wording():
    source = inspect.getsource(api._plan_final_tool_pool)

    assert "Final Tool Selector marked this Registry candidate" not in source
    assert "Registry candidate was recalled" in source
    assert "no post-recall semantic" in source
    assert "recall_union_no_llm" in source


def test_first_round_semantic_judge_tool_augmentation_flow_is_unchanged():
    source = inspect.getsource(api._plan_tool_pool_patch_from_responsibility_feedback)

    assert "_creator_responsibility_feedback_recall_query" in source
    assert "_recall_creator_tool_candidates" in source
    assert "responsibility_feedback" in source
    assert "candidate_tool_catalog" in source
