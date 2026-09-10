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


def test_contractless_skill_becomes_an_ordinary_complete_create_request(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = _write_skill(tmp_path, complete=False)
    skill.joinpath("SKILL.md").write_text(
        "该 Skill 可以比较两个 CSV 文件，并生成比较报告",
        encoding="utf-8",
    )
    complete_requirement = (
        "实现一个 CSV 比较工具：\n"
        "1. 比较两个 CSV 文件；\n"
        "2. 生成比较报告；\n"
        "3. 输出差异总结。"
    )

    async def fake_call(messages, role, **kwargs):
        assert role == "planner"
        prompt = messages[0]["content"]
        assert "你是 Skill 需求整理器" in prompt
        assert "请将两者合并，形成当前需要实现的完整功能需求" in prompt
        assert "输出面向 Skill 创建，不面向修改过程" in prompt
        assert "不需要描述需求来源" in prompt
        assert "保证已有能力和新增需求都被正确包含" in prompt
        payload = json.loads(messages[1]["content"])
        assert payload == {
            "skill_md": "该 Skill 可以比较两个 CSV 文件，并生成比较报告",
            "new_requirement": "增加差异总结能力",
        }
        return json.dumps({
            "skill_summary": {
                "goal": "比较 CSV",
                "capabilities": ["比较", "报告", "差异总结"],
                "inputs": ["两个 CSV 文件"],
                "outputs": ["比较报告", "差异总结"],
            },
            "complete_requirement": complete_requirement,
        })

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    request = api.PreparePlanRequest(
        mode="revise",
        skill_name="demo",
        user_request="增加差异总结能力",
        conversation_history=[{"role": "user", "content": "不得透传"}],
        human_feedback="不得透传",
        previous_blueprint_text="不得透传",
        function_items=[{"id": "不得透传"}],
        responsibility_edges=[{"source": "不得透传"}],
        requirement_allocations=[{"requirement": "不得透传"}],
        interface_contracts={"不得透传": True},
    )

    result = asyncio.run(api._preprocess_existing_skill_request(request))

    assert result.mode == "create"
    assert result.user_request == complete_requirement
    assert result.conversation_history == []
    assert result.human_feedback == ""
    assert result.previous_blueprint_text == ""
    assert result.function_items is None
    assert result.responsibility_edges is None
    assert result.requirement_allocations is None
    assert result.interface_contracts is None
    for forbidden in ("修改已有 Skill", "保留旧实现", "增量调整", "patch", "resume"):
        assert forbidden not in result.user_request

    direct_create = api.PreparePlanRequest(
        mode="create",
        skill_name="demo",
        user_request=complete_requirement,
    )
    assert result == direct_create


def test_contractless_and_direct_requirements_are_identical_before_planner(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = _write_skill(tmp_path, complete=False)
    skill.joinpath("SKILL.md").write_text("CSV比较工具，可以生成报告", encoding="utf-8")
    direct_requirement = "实现CSV比较工具，支持比较、报告、差异总结"

    async def fake_call(messages, role, **kwargs):
        return json.dumps({
            "skill_summary": {
                "goal": "处理并比较 CSV",
                "capabilities": ["比较", "报告", "差异总结"],
                "inputs": ["CSV 文件"],
                "outputs": ["报告", "差异总结"],
            },
            "complete_requirement": direct_requirement,
        })

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    extracted = asyncio.run(api._preprocess_existing_skill_request(api.PreparePlanRequest(
        mode="revise",
        skill_name="demo",
        user_request="增加差异总结",
    )))
    direct = api.PreparePlanRequest(
        mode="create",
        skill_name="demo",
        user_request=direct_requirement,
    )

    assert extracted.user_request == direct.user_request
    assert extracted == direct


def test_blueprint_planner_rejects_unprocessed_edit_request():
    request = api.PreparePlanRequest(mode="revise", skill_name="demo", user_request="增加导出")
    try:
        asyncio.run(api._generate_internal_blueprint_or_questions(request))
    except api.PreparePlanProtocolError as exc:
        assert str(exc) == "Blueprint Planner requires mode=create"
    else:
        raise AssertionError("edit request reached the create-only Blueprint Planner")


def test_blueprint_planner_contains_no_edit_context_branch():
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "existing_skill_context" not in source
    assert "revise" not in source.lower()
    assert "edit" not in source.lower()


def test_frozen_interface_contract_is_carried_verbatim_without_graph_projection():
    frozen = {
        "contract_version": "planner-v1",
        "interfaces": [{"contract_id": "opaque", "runtime": {"argv": ["source"]}}],
    }
    carried = api._carry_frozen_interface_contracts(frozen)
    assert carried == frozen
    assert carried is not frozen
    assert carried["interfaces"] is not frozen["interfaces"]


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
