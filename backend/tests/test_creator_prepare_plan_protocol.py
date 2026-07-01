import pytest
from fastapi import HTTPException

from backend.services.creator import api
from backend.services.creator.common import AnalyzeBlueprintResponse, FileSpecOut


def _request(**kwargs):
    data = {"user_request": "做一个工具", "human_feedback": "", "model": None}
    data.update(kwargs)
    return api.PreparePlanRequest(**data)


def _ready_blueprint(paths="- path: `SKILL.md`\n  role: skill_overview\n  inputs: [user_request]\n  outputs: [workflow]\n  dependencies: []\n  required_capabilities: []\n  forbidden_capabilities: [hidden_runtime_protocol]\n  references: []"):
    return f"""## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: demo-skill
### I/O 契约
- **输入**: runtime text
- **输出**: JSON
- **触发词**: 处理文本
### 目录结构
[当前 Skill 根目录]
├── SKILL.md
### 工作流逻辑
1. 处理输入
### SkillPlan / 文件职责计划
{paths}
### 宿主执行方式
- **直接回答**: 返回结果
- **需要脚本/命令**: 无
- **禁止隐式执行**: 遵守
- **执行后回答**: 返回
### 资源清单
- [ ] 无
"""


def _plan(path="SKILL.md", assets=None):
    return AnalyzeBlueprintResponse(
        skill_name="demo-skill",
        files=[FileSpecOut(path=path, purpose="overview", required=True, can_skip=False)],
        warnings=[],
        asset_requirements=assets or [],
        blueprint_text=_ready_blueprint(),
    )


def test_prepare_question_protocol_helpers_require_options_and_supplement():
    questions = ["输入来源希望支持哪种？A. 粘贴文本 B. 上传文件", "还有其他需要补充的要求吗？A. 没有 B. 有，我补充说明"]
    assert api._prepare_questions_have_options(questions)
    assert api._prepare_questions_include_supplement_check(questions)


def test_prepare_questions_missing_supplement_are_completed():
    questions = api._normalize_prepare_clarifying_questions(["输出格式希望是哪种？A. JSON B. Markdown"])
    assert len(questions) == 2
    assert "补充" in questions[-1]
    assert api._prepare_questions_have_options(questions)


def test_preflight_rejects_asset_placeholder_and_directory_paths():
    assert any(i["code"] == "invalid_asset_placeholder_path" for i in api._preflight_prepare_blueprint_text(_ready_blueprint("- path: `assets/<name.ext>`\n  role: asset\n  source: user_upload")))
    assert any(i["code"] == "invalid_asset_directory_path" for i in api._preflight_prepare_blueprint_text(_ready_blueprint("- path: `assets/`\n  role: asset\n  source: user_upload")))


def test_preflight_rejects_runtime_input_assets_and_missing_skillplan_path():
    text = _ready_blueprint("- path: `SKILL.md`\n  role: skill_overview") + "\n运行时每次上传的用户输入文件 assets/input.pdf\n"
    codes = {i["code"] for i in api._preflight_prepare_blueprint_text(text)}
    assert "directory_or_text_path_missing_from_skill_plan" in codes


@pytest.mark.asyncio
async def test_needs_clarification_response_has_options_and_supplement(monkeypatch):
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输入是什么？"]}
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    resp = await api.prepare_plan(_request())
    assert resp.status == "needs_clarification"
    assert api._prepare_questions_have_options(resp.clarifying_questions)
    assert "补充" in resp.clarifying_questions[-1]


@pytest.mark.asyncio
async def test_asset_placeholder_ready_is_translated_to_blocked_not_http_400(monkeypatch):
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint("- path: `assets/<name.ext>`\n  role: asset\n  source: user_upload")}
    async def no_repair(**kwargs):
        return kwargs["blueprint_text"]
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_repair_prepare_blueprint_protocol", no_repair)
    resp = await api.prepare_plan(_request())
    assert resp.status == "blocked"


@pytest.mark.asyncio
async def test_analyze_blueprint_error_is_translated_to_blocked(monkeypatch):
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint()}
    async def fake_analyze(_request):
        raise HTTPException(status_code=400, detail="invalid blueprint")
    async def no_repair(**kwargs):
        return kwargs["blueprint_text"]
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    monkeypatch.setattr(api, "_repair_prepare_blueprint_protocol", no_repair)
    resp = await api.prepare_plan(_request())
    assert resp.status == "blocked"


@pytest.mark.asyncio
async def test_ready_uses_strict_analyze_plan_and_filters_dynamic_paths(monkeypatch):
    calls = []
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint()}
    async def fake_analyze(request):
        calls.append(request)
        return _plan(path="SKILL.md")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    resp = await api.prepare_plan(_request(human_feedback="A. 没有，按上面的选择继续"))
    assert resp.status == "ready"
    assert calls and calls[0].strict is True
    assert resp.review_summary.files_to_create_or_update == ["SKILL.md"]
    assert not any("<" in p or p == "assets/" for p in resp.review_summary.files_to_create_or_update)
    assert resp.review_summary.assets_to_upload == []
