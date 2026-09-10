import asyncio
import inspect
import json

from backend.services.creator import api


def _write_skill(root, *, complete=True):
    skill = root / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo\n已有能力", encoding="utf-8")
    if complete:
        creator = skill / ".creator"
        creator.mkdir()
        (creator / "blueprint.md").write_text("# Blueprint", encoding="utf-8")
        for name in ("requirement_graph.json", "creation_plan.json", "interface_contracts.json"):
            (creator / name).write_text("{}", encoding="utf-8")
    return skill


def test_existing_skill_design_requires_all_four_contract_files(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = _write_skill(tmp_path)
    assert api._load_existing_skill_design("demo")["contract_complete"] is True
    (skill / ".creator" / "interface_contracts.json").unlink()
    loaded = api._load_existing_skill_design("demo")
    assert loaded["contract_complete"] is False
    assert loaded["skill_md"].startswith("# Demo")


def test_edit_preprocessor_crosses_into_clean_create_request(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    _write_skill(tmp_path)

    async def fake_call(messages, role, **kwargs):
        assert "existing_design" in messages[1]["content"]
        return json.dumps({"change_analysis": {}, "complete_requirement": "完整的新需求"})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    request = api.PreparePlanRequest(mode="revise", skill_name="demo", user_request="增加导出")
    result = asyncio.run(api._preprocess_existing_skill_request(request))
    assert result.mode == "create"
    assert result.user_request == "完整的新需求"
    assert result.previous_blueprint_text == ""
    assert result.function_items is None
    assert result.interface_contracts is None


def test_blueprint_planner_rejects_unprocessed_edit_request():
    request = api.PreparePlanRequest(mode="revise", skill_name="demo", user_request="增加导出")
    try:
        asyncio.run(api._generate_internal_blueprint_or_questions(request))
    except api.PreparePlanProtocolError as exc:
        assert "only accepts create requests" in str(exc)
    else:
        raise AssertionError("edit request reached the create-only Blueprint Planner")


def test_blueprint_planner_contains_no_edit_context_branch():
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "existing_skill_context" not in source
    assert "revise" not in source.lower()


def test_snapshot_persists_the_four_edit_contracts(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    api._persist_creator_design_snapshot(
        skill_name="demo",
        blueprint_text="# Final Blueprint",
        requirement_graph={"function_items": [], "responsibility_edges": []},
        creation_plan={"status": "ready"},
        interface_contracts={"interfaces": [{"contract_id": "frozen-contract-1"}]},
    )
    creator = tmp_path / "demo" / ".creator"
    assert {path.name for path in creator.iterdir()} == set(api._CREATOR_DESIGN_FILES)
    assert json.loads((creator / "creation_plan.json").read_text())["status"] == "ready"
    assert json.loads((creator / "interface_contracts.json").read_text()) == {
        "interfaces": [{"contract_id": "frozen-contract-1"}]
    }
