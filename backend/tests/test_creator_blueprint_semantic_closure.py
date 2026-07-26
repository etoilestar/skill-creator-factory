import json

import pytest

from backend.services.creator import api
from backend.services.creator.contracts import validate_requirement_allocations


def _allocation(requirement_id, owners):
    return {
        "requirement_id": requirement_id,
        "requirement": f"完成 {requirement_id}",
        "owners": owners,
        "evidence": {"responsibility": "完成责任", "outputs": ["result"], "capabilities": ["operation"]},
    }


def test_one_function_item_may_own_multiple_requirements():
    result = validate_requirement_allocations(
        [_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/a.py"])],
        frozen_file_plan_paths=["scripts/a.py"],
    )
    assert [item["owners"] for item in result] == [["scripts/a.py"], ["scripts/a.py"]]


def test_multiple_function_items_may_cooperate_on_one_requirement():
    result = validate_requirement_allocations(
        [_allocation("R1", ["scripts/a.py", "scripts/b.py"])],
        frozen_file_plan_paths=["scripts/a.py", "scripts/b.py"],
    )
    assert result[0]["owners"] == ["scripts/a.py", "scripts/b.py"]


def test_invalid_owner_is_rejected_without_path_guessing():
    with pytest.raises(ValueError, match="outside frozen FilePlan domain"):
        validate_requirement_allocations(
            [_allocation("R1", ["scripts/not_exists.py"])],
            frozen_file_plan_paths=["scripts/exists.py"],
        )


def test_requirement_ids_are_unique_and_requirements_non_empty():
    with pytest.raises(ValueError, match="duplicate requirement_id"):
        validate_requirement_allocations(
            [_allocation("R1", ["scripts/a.py"]), _allocation("R1", ["scripts/a.py"])],
            frozen_file_plan_paths=["scripts/a.py"],
        )
    blank = _allocation("R2", ["scripts/a.py"])
    blank["requirement"] = ""
    with pytest.raises(ValueError, match="requirement must be non-empty"):
        validate_requirement_allocations([blank], frozen_file_plan_paths=["scripts/a.py"])


@pytest.mark.asyncio
async def test_generic_missing_core_requirement_is_reviewer_failure(monkeypatch):
    async def complete(messages, *_args, **_kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["user_requirement"] == "用户需要完成 A、B、C 三项责任"
        assert len(payload["requirement_allocations"]) == 2
        return json.dumps({"passed": False, "issues": [{
            "issue_type": "requirement_uncovered", "requirement_id": "R3",
            "affected_targets": [], "reason": "C 没有责任 owner", "repair_guidance": "补足 C 的真实责任",
        }]})
    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="用户需要完成 A、B、C 三项责任"),
        blueprint_text="blueprint", function_items=[],
        requirement_allocations=[_allocation("R1", []), _allocation("R2", [])], planner_model="test",
    )
    assert review["passed"] is False
    assert review["issues"][0]["issue_type"] == "requirement_uncovered"


@pytest.mark.asyncio
async def test_resource_semantic_conflict_is_reported_by_reviewer_not_suffix_logic(monkeypatch):
    async def complete(*_args, **_kwargs):
        return json.dumps({"passed": False, "issues": [{
            "issue_type": "resource_semantic_conflict", "requirement_id": "",
            "affected_targets": ["scripts/a.py"], "resource": "static/content.opaque",
            "reason": "声明需要既存静态内容但没有有效来源", "repair_guidance": "Re-evaluate the resource role/source/lifecycle.",
        }]})
    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="完成责任"), blueprint_text="dependency without provenance",
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", ["scripts/a.py"])], planner_model="test",
    )
    assert review["issues"][0]["resource"] == "static/content.opaque"


def test_summary_file_sets_are_authoritative_file_plan_projection():
    summary = api.PreparePlanReviewSummary(
        files_to_create_or_update=["extra.file"], assets_to_upload=["wrong.asset"]
    )
    files = [
        api.FileSpecOut(path="scripts/a.py", purpose="work", file_type="script", required=True, can_skip=False),
        api.FileSpecOut(path="resources/upload.opaque", purpose="static", file_type="asset", asset_source="user_upload", required=True, can_skip=False),
        api.FileSpecOut(path="references/info.opaque", purpose="guidance", file_type="reference", asset_source="user_upload", required=True, can_skip=False),
    ]
    api._sync_prepare_summary_files_from_skill_plan(summary, files)
    assert summary.files_to_create_or_update == [item.path for item in files]
    assert summary.assets_to_upload == ["resources/upload.opaque"]


@pytest.mark.asyncio
async def test_localized_replan_prompt_requires_minimal_change(monkeypatch):
    calls = []
    async def complete(messages, *_args, **_kwargs):
        calls.append(messages)
        return json.dumps({"internal_blueprint_text": "replanned"})
    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    monkeypatch.setattr(api, "validate_blueprint_shape_for_creator", lambda _text: None)
    monkeypatch.setattr(api, "_preflight_prepare_blueprint_text", lambda _text: [])
    result = await api._replan_blueprint_for_semantic_closure(
        request=api.PreparePlanRequest(user_request="完成 A、B、C"), blueprint_text="original",
        function_items=[], requirement_allocations=[], blocking_issues=[{
            "issue_type": "requirement_uncovered", "affected_targets": [],
        }], planner_model="test",
    )
    assert result == "replanned"
    assert len(calls) == 1
    assert "minimum Blueprint facts" in calls[0][0]["content"]
