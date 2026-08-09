from backend.services.creator.frozen_facts import (
    FACT_OWNERS,
    CreatorFactsSnapshot,
    frozen_fact_digests,
    project_frozen_facts_to_summary,
)
import pytest


def test_summary_projection_rejects_model_owned_frozen_identities():
    projected = project_frozen_facts_to_summary(
        summary_prose={
            "goal": "summarized goal",
            "files_to_create_or_update": ["A", "B", "C"],
            "assets_to_upload": ["asset_a.bin"],
        },
        authoritative_files=["A", "B"],
        authoritative_upload_assets=[],
    )
    assert projected["files_to_create_or_update"] == ["A", "B"]
    assert projected["assets_to_upload"] == []


def test_key_frozen_facts_have_one_owner_stage():
    assert FACT_OWNERS["file_plan"] == "blueprint_freeze"
    assert FACT_OWNERS["function_items"] == "blueprint_freeze"
    assert FACT_OWNERS["resource_authority"] == "resource_authority"
    assert FACT_OWNERS["review_summary_prose"] == "review_summary"
    assert len(FACT_OWNERS) == len(set(FACT_OWNERS))


def test_function_item_digest_ignores_order_and_presentation_metadata():
    first = CreatorFactsSnapshot(function_items=({
        "target_file": "unit_a",
        "purpose": "produce   result",
        "inputs": ["slot_y", "slot_x"],
        "outputs": ["result"],
        "dependencies": ["unit_b"],
        "goal": "presentation one",
    },))
    second = CreatorFactsSnapshot(function_items=({
        "target_file": "unit_a",
        "purpose": "produce result",
        "inputs": ["slot_x", "slot_y"],
        "outputs": ["result"],
        "dependencies": ["unit_b"],
        "goal": "presentation two",
    },))
    assert frozen_fact_digests(first)["function_items"] == frozen_fact_digests(second)["function_items"]


@pytest.mark.asyncio
async def test_review_summary_model_schema_is_prose_only(monkeypatch):
    from backend.services.creator import api

    captured = {}
    file_specs = [
        api.FileSpecOut(path="A", purpose="one", file_type="script", required=True, can_skip=False),
        api.FileSpecOut(path="B", purpose="two", file_type="reference", required=True, can_skip=False),
    ]
    monkeypatch.setattr(
        api, "parse_blueprint",
        lambda *_args, **_kwargs: type("FrozenPlan", (), {"files": file_specs})(),
    )
    monkeypatch.setattr(
        api, "route_model", lambda *_args, **_kwargs: type("Route", (), {"model": "test"})(),
    )

    async def complete(**kwargs):
        captured["schema"] = kwargs["response_schema"]
        return {"goal": "goal", "input": "input", "output": "output",
                "workflow": [], "risks": [], "changes": []}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", complete)
    summary = await api._project_prepare_review_summary_from_blueprint(
        request=api.PreparePlanRequest(user_request="abstract"),
        blueprint_text="structured blueprint",
    )
    assert "files_to_create_or_update" not in captured["schema"]["properties"]
    assert "assets_to_upload" not in captured["schema"]["properties"]
    assert summary.files_to_create_or_update == ["A", "B"]
    assert summary.assets_to_upload == []
