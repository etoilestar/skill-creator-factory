from backend.services.creator.frozen_facts import (
    FACT_OWNERS,
    SNAPSHOT_AUTHORITATIVE_FIELDS,
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
    assert SNAPSHOT_AUTHORITATIVE_FIELDS <= FACT_OWNERS.keys()
    assert set(CreatorFactsSnapshot.__dataclass_fields__) == SNAPSHOT_AUTHORITATIVE_FIELDS


def test_snapshot_defensively_copies_mutable_owner_outputs():
    items = [{"target_file": "unit_a", "inputs": ["slot_x"]}]
    resources = {"authoritative_upload_assets": ["resource_a"]}
    snapshot = CreatorFactsSnapshot.from_mutable(
        function_items=items, resource_authority=resources,
    )
    items[0]["inputs"].append("mutated")
    resources["authoritative_upload_assets"].append("mutated_resource")
    assert snapshot.function_items[0]["inputs"] == ["slot_x"]
    assert snapshot.authoritative_upload_assets == ("resource_a",)


def test_upload_assets_are_not_inferred_from_file_plan_path():
    snapshot = CreatorFactsSnapshot.from_mutable(
        file_plan=[{"path": "assets/resource_a.bin", "file_type": "asset"}],
        resource_authority={"authoritative_assets": ["assets/resource_a.bin"],
                            "authoritative_upload_assets": []},
    )
    projected = project_frozen_facts_to_summary(
        summary_prose={}, authoritative_files=list(snapshot.authoritative_files),
        authoritative_upload_assets=list(snapshot.authoritative_upload_assets),
    )
    assert projected["assets_to_upload"] == []


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
    monkeypatch.setattr(
        api, "route_model", lambda *_args, **_kwargs: type("Route", (), {"model": "test"})(),
    )
    monkeypatch.setattr(
        api, "parse_blueprint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Summary must not parse Blueprint identities")
        ),
    )

    async def complete(**kwargs):
        captured["schema"] = kwargs["response_schema"]
        return {"goal": "goal", "input": "input", "output": "output",
                "workflow": [], "risks": [], "changes": []}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", complete)
    snapshot = CreatorFactsSnapshot.from_mutable(
        file_plan=[{"path": "A"}, {"path": "B"}],
        resource_authority={"authoritative_upload_assets": []},
    )
    summary = await api._project_prepare_review_summary(
        request=api.PreparePlanRequest(user_request="abstract"),
        facts_snapshot=snapshot,
        blueprint_explanatory_text="structured blueprint",
    )
    assert "files_to_create_or_update" not in captured["schema"]["properties"]
    assert "assets_to_upload" not in captured["schema"]["properties"]
    assert summary.files_to_create_or_update == ["A", "B"]
    assert summary.assets_to_upload == []
