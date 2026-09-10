import asyncio
import inspect
import json
import logging

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
    skill_md = """# CSV 比较工具

使用 scripts/main.py 比较 CSV，读取 references/schema.json，
并将结果写入 outputs/result.json。
"""
    skill.joinpath("SKILL.md").write_text(skill_md, encoding="utf-8")
    complete_requirement = (
        "创建一个 CSV 比较 Skill：接收两个 CSV 文件，比较数据差异，"
        "并输出包含逐项差异及差异总结的比较结果。"
    )

    async def fake_call(messages, role, **kwargs):
        assert role == "planner"
        prompt = messages[0]["content"]
        assert "最终要创建的 Skill" in prompt
        assert '"complete_requirement"' in prompt
        assert '"skill_capability_summary"' in prompt
        for summary_field in ("goal", "capabilities", "inputs", "outputs"):
            assert f'"{summary_field}"' in prompt
        assert "已有 Skill 描述" in prompt
        assert "功能能力参考" in prompt
        assert "用户新增需求" in prompt
        assert "新目标需求" in prompt
        for forbidden_semantics in (
            "修改已有 Skill",
            "增加某功能",
            "扩展已有能力",
            "基于原实现",
            "基于已有实现",
            "保留旧实现",
            "保持原实现",
            "完整复制旧 Skill",
        ):
            assert forbidden_semantics in prompt
        for forbidden_implementation_detail in (
            "原 SKILL.md 文件结构",
            "scripts 文件路径",
            "references 文件",
            "assets 文件",
            "schema 文件",
            "output 目录",
            "runtime 参数",
            "原接口名称",
            "原函数名称",
            "原代码组织方式",
        ):
            assert forbidden_implementation_detail in prompt
        assert "不要说明任何内容的来源或形成过程" in prompt
        assert "迁移" not in prompt
        payload = json.loads(messages[1]["content"])
        assert payload == {
            "skill_md": skill_md,
            "new_requirement": "增加差异总结",
        }
        return json.dumps({
            "skill_capability_summary": {
                "goal": "比较 CSV 数据",
                "capabilities": ["CSV 比较"],
                "inputs": ["CSV 文件"],
                "outputs": ["比较结果"],
            },
            "complete_requirement": complete_requirement,
        })

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    request = api.PreparePlanRequest(
        mode="revise",
        skill_name="demo",
        user_request="增加差异总结",
        conversation_history=[{"role": "user", "content": "不得透传"}],
        human_feedback="不得透传",
        previous_blueprint_text="不得透传",
        prepare_action="submit_supplement",
        uploaded_files=[{"path": "inputs/source.csv"}],
        model="test-model",
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
    assert result.prepare_action == "none"
    assert result.skill_name is None
    assert result.uploaded_files == [{"path": "inputs/source.csv"}]
    assert result.model == "test-model"
    assert result.function_items is None
    assert result.responsibility_edges is None
    assert result.requirement_allocations is None
    assert result.interface_contracts is None
    for forbidden in (
        "修改已有 Skill",
        "增加某功能",
        "扩展已有能力",
        "基于原实现",
        "保留旧实现",
        "scripts/main.py",
        "references/schema.json",
        "outputs/result.json",
    ):
        assert forbidden not in result.user_request
    assert "CSV 比较" in result.user_request
    assert "差异总结" in result.user_request

    direct_create = api.PreparePlanRequest(
        mode="create",
        user_request=complete_requirement,
        uploaded_files=[{"path": "inputs/source.csv"}],
        model="test-model",
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
        user_request=direct_requirement,
    )

    assert extracted.user_request == direct.user_request
    assert extracted == direct


def test_preprocessor_debug_logs_complete_direct_and_contractless_requests(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    _write_skill(tmp_path, complete=False)

    async def fake_call(messages, role, **kwargs):
        return json.dumps({"complete_requirement": "完整创建需求"})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    direct = api.PreparePlanRequest(
        mode="create",
        user_request="完整创建需求",
        uploaded_files=[{"path": "inputs/source.csv"}],
        model="test-model",
    )
    contractless = api.PreparePlanRequest(
        mode="revise",
        skill_name="demo",
        user_request="增加导出",
        uploaded_files=[{"path": "inputs/source.csv"}],
        model="test-model",
    )

    with caplog.at_level(logging.DEBUG, logger=api.logger.name):
        direct_result = asyncio.run(api._preprocess_existing_skill_request(direct))
        contractless_result = asyncio.run(api._preprocess_existing_skill_request(contractless))

    assert direct_result == contractless_result
    expected_payload = direct.model_dump_json()
    matching_records = [
        record for record in caplog.records
        if record.getMessage() == (
            "[Creator][existing_skill_preprocess][return_request] "
            f"request={expected_payload}"
        )
    ]
    assert len(matching_records) == 2


def test_prepare_plan_entry_debug_logs_complete_request(monkeypatch, caplog):
    request = api.PreparePlanRequest(
        mode="create",
        user_request="创建 CSV 比较 Skill",
        conversation_history=[{"role": "user", "content": "上下文"}],
        uploaded_files=[{"path": "inputs/source.csv"}],
        human_feedback="补充要求",
        model="test-model",
    )
    sentinel = object()

    async def fake_preprocess(received):
        assert received is request
        return received

    async def fake_prepare(received):
        assert received is request
        return sentinel

    monkeypatch.setattr(api, "_preprocess_existing_skill_request", fake_preprocess)
    monkeypatch.setattr(api, "_prepare_plan_impl", fake_prepare)

    with caplog.at_level(logging.DEBUG, logger=api.logger.name):
        result = asyncio.run(api.prepare_plan(request))

    assert result is sentinel
    assert caplog.records[0].getMessage() == (
        "[Creator][prepare_plan][entry_request] "
        f"request={request.model_dump_json()}"
    )


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
