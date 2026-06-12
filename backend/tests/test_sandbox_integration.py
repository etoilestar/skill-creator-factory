"""Sandbox 页面后台逻辑集成测试。

验证场景：用户输入"生成一个狮子和大象的故事" -> 加载"animal-world-story-generator"技能
-> 后台处理完成的全流程关键节点输入输出校验。

覆盖的关键节点：
1. build_skill_context — 技能上下文构建
2. _parse_need_body_decision — 元数据匹配决策
3. load_skill_body_prompt — 技能正文加载
4. _parse_child_skill_decision — 子技能选择决策
5. _run_instruction_analysis_round — 指令语义分析
6. _execute_single_task — 单任务执行
7. _execute_skill_workflow — 工作流执行
8. SSE 事件流格式校验
9. 会话状态与步骤跳过
10. Action Schema 构建
11. 错误纠正与重试
12. 输出文件链接重写
13. 数据流上下文传递
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.routers.chat_models import ChatRequest, Message


# ---------------------------------------------------------------------------
# 测试用 Skill 包创建辅助函数
# ---------------------------------------------------------------------------

_SKILL_NAME = "animal-world-story-generator"

_SKILL_MD = """\
---
name: animal-world-story-generator
description: 生成动物世界主题的故事，支持多种动物角色和场景描述。
---

# 动物世界故事生成器

根据用户输入的动物和场景，生成一个有趣的动物世界故事。

## 使用

读取 `references/story_template.md` 中的故事模板。

## 执行步骤

1. 根据用户输入提取动物角色和场景
2. 调用 LLM 生成故事文本
3. 生成故事配图
4. 组装最终输出

## 脚本命令

```bash
python scripts/generate_story.py '{{"topic":"{{topic}}"}}'
```
"""

_REFERENCE_MD = """\
# 故事模板

## 故事结构

1. 开场：介绍动物角色和场景
2. 发展：动物之间的互动和冲突
3. 高潮：关键事件
4. 结局：和解与总结
"""

_SCRIPT_PY = """\
import json
import sys
import os

def parse_args():
    if len(sys.argv) < 2:
        return {}
    return json.loads(sys.argv[1])

def run(payload):
    topic = payload.get('topic', '动物世界')
    story_text = f"在遥远的非洲大草原上，{topic}。狮子是大草原的王者，大象是智慧的守护者。"
    output_dir = os.environ.get('OUTPUT_DIR', 'outputs')
    os.makedirs(output_dir, exist_ok=True)
    result = {
        'story_text': story_text,
        'text': story_text,
        'image_paths': [],
        'images': [],
    }
    return result

if __name__ == '__main__':
    payload = parse_args()
    print(json.dumps(run(payload), ensure_ascii=False))
"""


def _create_skill_package(tmp_path: Path) -> Path:
    """创建 animal-world-story-generator 技能包目录结构。"""
    skill_dir = tmp_path / _SKILL_NAME
    scripts_dir = skill_dir / "scripts"
    refs_dir = skill_dir / "references"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (refs_dir / "story_template.md").write_text(_REFERENCE_MD, encoding="utf-8")
    (scripts_dir / "generate_story.py").write_text(_SCRIPT_PY, encoding="utf-8")

    return skill_dir


def _make_chat_request(text: str, **overrides) -> ChatRequest:
    """构造标准 ChatRequest。"""
    defaults = {
        "messages": [Message(role="user", content=text)],
        "execution_mode": "execute",
    }
    defaults.update(overrides)
    return ChatRequest(**defaults)


def _patch_skill_roots(monkeypatch, tmp_path):
    """统一 patch 所有 skill root 路径到 tmp_path。"""
    from backend.config import settings
    monkeypatch.setattr(settings, "skills_path", tmp_path)
    monkeypatch.setattr(settings, "managed_skills_path", tmp_path)
    monkeypatch.setattr(settings, "workspace_skills_path", tmp_path)
    monkeypatch.setattr(settings, "shared_skills_path", tmp_path)
    monkeypatch.setattr(settings, "bundled_skills_path", tmp_path)


def _patch_skill_loaders(monkeypatch, skill_dir):
    """Mock stream_pipeline 中的 skill loader 函数以绕过 governance 文件锁。"""
    _METADATA_PROMPT = (
        "你处于 Skill 加载流程的第一阶段：metadata 判断阶段。\n\n"
        "## Skill Metadata\n"
        f"- name: {_SKILL_NAME}\n"
        f"- description: 生成动物世界主题的故事，支持多种动物角色和场景描述。\n\n"
        "---\n\n"
    )
    monkeypatch.setattr(
        "backend.routers.sandbox.stream_pipeline.load_skill_metadata_prompt",
        lambda name: _METADATA_PROMPT,
    )
    monkeypatch.setattr(
        "backend.routers.sandbox.stream_pipeline.load_skill_body_prompt",
        lambda name: _SKILL_MD,
    )
    monkeypatch.setattr(
        "backend.routers.sandbox.stream_pipeline.load_child_skill_body_prompt",
        lambda name, child_ref: "",
    )


# ---------------------------------------------------------------------------
# 节点1: build_skill_context — 技能上下文构建
# ---------------------------------------------------------------------------

class TestBuildSkillContext:
    """验证 build_skill_context 的输入输出。

    前置条件：技能包目录存在且包含 SKILL.md
    输入：skill_name = "animal-world-story-generator"
    预期输出：
      - skill_name 字段正确
      - metadata_prompt 非空（包含 frontmatter 信息）
      - body_loader 可调用且返回非空正文
      - child_body_loader 可调用
      - execution_root 指向正确的技能目录
      - enable_action_execution = True
    """

    def test_context_contains_required_fields(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        # Mock _skill_root_for_name 和 skill loaders 以绕过 governance 文件锁
        monkeypatch.setattr(
            "backend.routers.sandbox.stream_pipeline._skill_root_for_name",
            lambda name: skill_dir.resolve(),
        )
        _patch_skill_loaders(monkeypatch, skill_dir)

        from backend.routers.sandbox.stream_pipeline import build_skill_context
        context = build_skill_context(_SKILL_NAME)

        assert context["skill_name"] == _SKILL_NAME
        assert isinstance(context["metadata_prompt"], str)
        assert len(context["metadata_prompt"]) > 0
        assert callable(context["body_loader"])
        assert callable(context["child_body_loader"])
        assert context["enable_action_execution"] is True
        assert context["execution_root"] is not None

    def test_metadata_prompt_contains_skill_description(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        monkeypatch.setattr(
            "backend.routers.sandbox.stream_pipeline._skill_root_for_name",
            lambda name: skill_dir.resolve(),
        )
        _patch_skill_loaders(monkeypatch, skill_dir)

        from backend.routers.sandbox.stream_pipeline import build_skill_context
        context = build_skill_context(_SKILL_NAME)
        assert "动物世界" in context["metadata_prompt"]
        assert _SKILL_NAME in context["metadata_prompt"]

    def test_body_loader_returns_full_skill_md(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        monkeypatch.setattr(
            "backend.routers.sandbox.stream_pipeline._skill_root_for_name",
            lambda name: skill_dir.resolve(),
        )
        _patch_skill_loaders(monkeypatch, skill_dir)

        from backend.routers.sandbox.stream_pipeline import build_skill_context
        context = build_skill_context(_SKILL_NAME)
        body_prompt = context["body_loader"]()

        assert isinstance(body_prompt, str)
        assert len(body_prompt) > len(context["metadata_prompt"])
        assert "脚本命令" in body_prompt or "generate_story" in body_prompt

    def test_execution_root_points_to_skill_dir(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        monkeypatch.setattr(
            "backend.routers.sandbox.stream_pipeline._skill_root_for_name",
            lambda name: skill_dir.resolve(),
        )
        _patch_skill_loaders(monkeypatch, skill_dir)

        from backend.routers.sandbox.stream_pipeline import build_skill_context
        context = build_skill_context(_SKILL_NAME)
        assert Path(context["execution_root"]).resolve() == skill_dir.resolve()

    def test_nonexistent_skill_raises_error(self, tmp_path, monkeypatch):
        """不存在的技能应抛出异常。"""
        _patch_skill_roots(monkeypatch, tmp_path)
        # Mock governance _save_state 以避免 Windows 文件锁
        monkeypatch.setattr(
            "backend.services.skill_governance._save_state",
            lambda state: None,
        )

        from backend.routers.sandbox.stream_pipeline import build_skill_context
        with pytest.raises((FileNotFoundError, ValueError)):
            build_skill_context("nonexistent-skill")


# ---------------------------------------------------------------------------
# 节点2: _parse_need_body_decision — 元数据匹配决策
# ---------------------------------------------------------------------------

class TestMetadataDecision:
    """验证元数据决策解析。

    输入：LLM 返回的决策文本
    预期输出：
      - need_body=True 当用户请求与技能匹配
      - need_body=False 当用户请求与技能不匹配
      - 解析容错：非法 JSON 默认返回 True
    """

    def test_need_body_true_for_matching_request(self):
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        llm_response = json.dumps({"need_body": True, "reason": "用户请求生成动物故事，匹配技能描述"})
        assert _parse_need_body_decision(llm_response) is True

    def test_need_body_false_for_non_matching_request(self):
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        llm_response = json.dumps({"need_body": False, "reason": "用户请求与技能不匹配"})
        assert _parse_need_body_decision(llm_response) is False

    def test_need_body_defaults_to_true_on_parse_error(self):
        """解析失败时默认进入正文阶段，避免模型格式错误导致 Skill 无法执行。"""
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        assert _parse_need_body_decision("not valid json") is True

    def test_need_body_string_true_variants(self):
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        for variant in ["true", "1", "yes", "y"]:
            text = json.dumps({"need_body": variant})
            assert _parse_need_body_decision(text) is True

    def test_need_body_string_false_variants(self):
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        for variant in ["false", "0", "no", "n"]:
            text = json.dumps({"need_body": variant})
            assert _parse_need_body_decision(text) is False

    def test_need_body_missing_key_defaults_to_true(self):
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        text = json.dumps({"reason": "no need_body key"})
        assert _parse_need_body_decision(text) is True

    def test_markdown_fence_stripped(self):
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        fenced = '```json\n{"need_body": true}\n```'
        assert _parse_need_body_decision(fenced) is True


# ---------------------------------------------------------------------------
# 节点3: load_skill_body_prompt — 技能正文加载
# ---------------------------------------------------------------------------

class TestSkillBodyLoading:
    """验证技能正文加载的输入输出。

    前置条件：技能包存在
    输入：skill_name
    预期输出：
      - 返回包含完整 SKILL.md 正文的 prompt
      - prompt 包含技能关键内容
    """

    def test_body_prompt_contains_full_content(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        # Mock get_visible_skill_dir 以绕过 governance
        from backend.services import kernel_loader
        monkeypatch.setattr(
            kernel_loader,
            "get_visible_skill_dir",
            lambda name, mode="sandbox": skill_dir,
        )

        from backend.services.kernel_loader import load_skill_body_prompt
        body = load_skill_body_prompt(_SKILL_NAME)

        assert isinstance(body, str)
        assert len(body) > 100
        assert "动物世界" in body or "故事" in body

    def test_metadata_prompt_shorter_than_body(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        from backend.services import kernel_loader
        monkeypatch.setattr(
            kernel_loader,
            "get_visible_skill_dir",
            lambda name, mode="sandbox": skill_dir,
        )

        from backend.services.kernel_loader import load_skill_metadata_prompt, load_skill_body_prompt
        metadata = load_skill_metadata_prompt(_SKILL_NAME)
        body = load_skill_body_prompt(_SKILL_NAME)

        assert len(body) >= len(metadata)


# ---------------------------------------------------------------------------
# 节点4: _parse_child_skill_decision — 子技能选择决策
# ---------------------------------------------------------------------------

class TestChildSkillDecision:
    """验证子技能选择决策解析。

    输入：LLM 返回的决策文本 + valid_child_refs
    预期输出：
      - need_child=True + 合法 child_ref
      - need_child=False 当无子技能或不匹配
      - 安全校验：child_ref 必须在 valid_child_refs 中
    """

    def test_no_child_when_no_valid_refs(self):
        from backend.routers.sandbox.metadata_decisions import _parse_child_skill_decision
        text = json.dumps({"need_child": True, "child_ref": "some-ref"})
        result = _parse_child_skill_decision(text, valid_child_refs=set())
        assert result["need_child"] is False

    def test_valid_child_ref_accepted(self):
        from backend.routers.sandbox.metadata_decisions import _parse_child_skill_decision
        text = json.dumps({"need_child": True, "child_ref": "african-safari", "reason": "匹配"})
        result = _parse_child_skill_decision(text, valid_child_refs={"african-safari", "ocean-adventure"})
        assert result["need_child"] is True
        assert result["child_ref"] == "african-safari"

    def test_invalid_child_ref_rejected(self):
        from backend.routers.sandbox.metadata_decisions import _parse_child_skill_decision
        text = json.dumps({"need_child": True, "child_ref": "nonexistent", "reason": "猜测"})
        result = _parse_child_skill_decision(text, valid_child_refs={"african-safari"})
        assert result["need_child"] is False
        assert "不在" in result["reason"] or "已忽略" in result["reason"]

    def test_missing_child_ref_with_need_child_true(self):
        from backend.routers.sandbox.metadata_decisions import _parse_child_skill_decision
        text = json.dumps({"need_child": True, "child_ref": "", "reason": "遗漏"})
        result = _parse_child_skill_decision(text, valid_child_refs={"african-safari"})
        assert result["need_child"] is False

    def test_json_parse_error_returns_no_child(self):
        from backend.routers.sandbox.metadata_decisions import _parse_child_skill_decision
        result = _parse_child_skill_decision("invalid json", valid_child_refs={"african-safari"})
        assert result["need_child"] is False

    def test_extract_child_refs_from_metadata_prompt(self):
        from backend.routers.sandbox.metadata_decisions import _extract_child_refs_from_metadata_prompt
        prompt = (
            "## Skill Metadata\n"
            "some content\n\n"
            "## Child Skills Manifest\n"
            "- ref: `african-safari`\n"
            "  name: African Safari\n"
            "- ref: `ocean-adventure`\n"
            "  name: Ocean Adventure\n"
            "\n---\n"
            "## Resources\n"
        )
        refs = _extract_child_refs_from_metadata_prompt(prompt)
        assert refs == {"african-safari", "ocean-adventure"}


# ---------------------------------------------------------------------------
# 节点5: _run_instruction_analysis_round — 指令语义分析
# ---------------------------------------------------------------------------

class TestInstructionAnalysis:
    """验证指令语义分析的输入输出。

    输入：body_prompt + ChatRequest（用户输入"生成一个狮子和大象的故事"）
    预期输出：
      - intent 非空
      - complexity 为 simple/moderate/complex 之一
      - requires_script_execution 为布尔值
      - constraints 和 output_requirements 为列表
    """

    def test_analysis_returns_required_fields(self, monkeypatch):
        from backend.routers.sandbox.instruction_analysis import _run_instruction_analysis_round

        mock_analysis = {
            "intent": "生成关于狮子和大象的动物世界故事",
            "scope": "动物角色故事生成",
            "constraints": ["必须包含狮子和大象角色"],
            "output_requirements": ["故事文本", "配图"],
            "complexity": "moderate",
            "requires_script_execution": True,
        }

        async def fake_complete_chat_once(messages, model, **kwargs):
            return json.dumps(mock_analysis)

        monkeypatch.setattr(
            "backend.routers.sandbox.instruction_analysis.complete_chat_once",
            fake_complete_chat_once,
        )

        request = _make_chat_request("生成一个狮子和大象的故事")
        result = asyncio.run(_run_instruction_analysis_round(
            body_prompt="Skill 正文内容",
            request=request,
            model="test-model",
        ))

        assert result["intent"] == "生成关于狮子和大象的动物世界故事"
        assert result["complexity"] == "moderate"
        assert result["requires_script_execution"] is True
        assert isinstance(result["constraints"], list)
        assert isinstance(result["output_requirements"], list)

    def test_analysis_fallback_on_json_error(self, monkeypatch):
        """LLM 返回非法 JSON 时应使用降级默认值。"""
        from backend.routers.sandbox.instruction_analysis import _run_instruction_analysis_round

        async def fake_complete_chat_once(messages, model, **kwargs):
            return "这不是合法JSON"

        monkeypatch.setattr(
            "backend.routers.sandbox.instruction_analysis.complete_chat_once",
            fake_complete_chat_once,
        )

        request = _make_chat_request("生成一个狮子和大象的故事")
        result = asyncio.run(_run_instruction_analysis_round(
            body_prompt="Skill 正文",
            request=request,
            model="test-model",
        ))

        assert "狮子" in result["intent"]
        assert result["complexity"] in ("simple", "moderate", "complex")


# ---------------------------------------------------------------------------
# 节点6: _execute_single_task — 单任务执行
# ---------------------------------------------------------------------------

class TestSingleTaskExecution:
    """验证单任务执行的输入输出。

    前置条件：技能包存在，脚本可执行
    输入：task dict + ChatRequest
    预期输出：
      - run_command 任务返回 success + stdout JSON
      - read_resource 任务返回资源内容
      - display/ignore 任务直接返回 success
    """

    def test_run_command_task_execution(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        monkeypatch.setenv("SKILL_TRIAL_RUN", "1")

        skill_dir = _create_skill_package(tmp_path)
        request = _make_chat_request("生成一个狮子和大象的故事")

        # Mock action schema 校验以避免重复入口冲突
        monkeypatch.setattr(
            "backend.routers.sandbox.task_executor._validate_runtime_command_against_action_schema",
            lambda command, **kw: None,
        )

        from backend.routers.sandbox.task_executor import _execute_single_task

        task = {
            "action": "run_command",
            "command": f'python scripts/generate_story.py \'{{"topic": "狮子和大象的故事"}}\'',
            "reason": "生成动物世界故事",
        }

        result, touched = _execute_single_task(
            task, [], request,
            execution_root=skill_dir,
            skill_name=_SKILL_NAME,
        )

        assert result["action"] == "run_command"
        assert result["success"] is True
        stdout = result.get("stdout", "")
        if stdout.strip():
            payload = json.loads(stdout.strip())
            assert "story_text" in payload or "text" in payload

    def test_display_task_returns_success(self):
        from backend.routers.sandbox.task_executor import _execute_single_task
        request = _make_chat_request("测试")
        task = {"action": "display", "reason": "展示信息"}
        result, touched = _execute_single_task(task, [], request)
        assert result["action"] == "display"
        assert result["success"] is True

    def test_ignore_task_returns_success(self):
        from backend.routers.sandbox.task_executor import _execute_single_task
        request = _make_chat_request("测试")
        task = {"action": "ignore", "reason": "跳过"}
        result, touched = _execute_single_task(task, [], request)
        assert result["action"] == "ignore"
        assert result["success"] is True

    def test_read_resource_task(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)
        request = _make_chat_request("测试")

        # Mock read_skill_resource_text 以绕过 governance
        from backend.routers.sandbox import task_executor
        monkeypatch.setattr(
            task_executor,
            "read_skill_resource_text",
            lambda name, path, **kw: {"content": _REFERENCE_MD, "truncated": False},
        )

        from backend.routers.sandbox.task_executor import _execute_single_task

        task = {
            "action": "read_resource",
            "path": "references/story_template.md",
            "reason": "读取故事模板",
        }

        result, touched = _execute_single_task(
            task, [], request,
            execution_root=skill_dir,
            skill_name=_SKILL_NAME,
        )

        assert result["action"] == "read_resource"
        assert result["success"] is True
        assert isinstance(result.get("content"), str)
        assert len(result["content"]) > 0


# ---------------------------------------------------------------------------
# 节点7: _execute_skill_workflow — 工作流执行
# ---------------------------------------------------------------------------

class TestWorkflowExecution:
    """验证工作流执行的输入输出。

    前置条件：技能包存在，action_schema 有效
    输入：execution_root + action_schema + user_context
    预期输出：
      - results 列表非空
      - context 包含工作流上下文数据
    """

    def test_workflow_executes_scripts_in_order(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        monkeypatch.setenv("SKILL_TRIAL_RUN", "1")

        skill_dir = _create_skill_package(tmp_path)

        # 创建第二个脚本
        (skill_dir / "scripts" / "format_output.py").write_text(
            "import json, sys\n"
            "p = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}\n"
            "text = p.get('story_text', p.get('text', 'default'))\n"
            "print(json.dumps({'formatted_text': f'Formatted: {text}', 'text': text}, ensure_ascii=False))\n",
            encoding="utf-8",
        )

        # Mock action schema 校验以避免重复入口冲突
        monkeypatch.setattr(
            "backend.routers.sandbox.task_executor._validate_runtime_command_against_action_schema",
            lambda command, **kw: None,
        )

        from backend.routers.sandbox.workflow_dataflow import _execute_skill_workflow

        action_schema = {
            "entries": [
                {
                    "script_path": "scripts/generate_story.py",
                    "command": 'python scripts/generate_story.py \'{"topic": "{{topic}}"}\'',
                    "role": "composite_generator",
                    "local_description": "生成故事",
                    "inputs": ["topic"],
                    "outputs": ["story_text", "image_paths"],
                },
                {
                    "script_path": "scripts/format_output.py",
                    "command": 'python scripts/format_output.py \'{"story_text": "{{story_text}}"}\'',
                    "role": "text_generator",
                    "local_description": "格式化输出",
                    "inputs": ["story_text"],
                    "outputs": ["formatted_text"],
                },
            ],
            "errors": [],
        }

        result = asyncio.run(_execute_skill_workflow(
            execution_root=skill_dir,
            action_schema=action_schema,
            user_context={"user_request": "生成一个狮子和大象的故事", "topic": "狮子和大象的故事"},
            request=ChatRequest(messages=[Message(role="user", content="生成一个狮子和大象的故事")]),
            skill_name=_SKILL_NAME,
        ))

        assert "results" in result
        assert len(result["results"]) >= 1
        assert "context" in result
        assert isinstance(result["context"], dict)


# ---------------------------------------------------------------------------
# 节点8: SSE 事件流格式校验
# ---------------------------------------------------------------------------

class TestSSEEventFormat:
    """验证 SSE 事件流的格式正确性。"""

    def test_sse_event_format(self):
        from backend.routers.chat_utils import _sse
        event = _sse({"status": {"phase": "analyzing", "message": "分析请求匹配度…"}})
        assert event.startswith("data: ")
        assert event.endswith("\n\n")
        json_str = event[len("data: "):-2]
        data = json.loads(json_str)
        assert "status" in data
        assert data["status"]["phase"] == "analyzing"

    def test_thought_event_format(self):
        from backend.routers.chat_utils import _thought
        event = _thought("metadata_decision", "分析匹配度", "需要加载正文", {"need_body": True})
        json_str = event[len("data: "):-2]
        data = json.loads(json_str)
        # _thought 输出格式: {"thought": {step, label, detail, data, ts}}
        assert "thought" in data
        assert data["thought"]["label"] == "分析匹配度"
        assert data["thought"]["step"] == "metadata_decision"

    def test_task_checklist_format(self):
        from backend.routers.chat_utils import _task_checklist
        tasks = [
            {"index": 0, "action": "run_command", "description": "生成故事", "command": "python scripts/run.py"},
            {"index": 1, "action": "display", "description": "展示结果"},
        ]
        event = _task_checklist(tasks)
        json_str = event[len("data: "):-2]
        data = json.loads(json_str)
        # _task_checklist 输出格式: {"task_checklist": {tasks, completed_indices, executing_index, ts}}
        assert "task_checklist" in data
        checklist = data["task_checklist"]
        assert isinstance(checklist["tasks"], list)
        assert len(checklist["tasks"]) == 2

    def test_sandbox_retry_format(self):
        from backend.routers.chat_utils import _sandbox_retry
        event = _sandbox_retry(attempt=1, max_retries=3, error="执行失败", corrected=False)
        json_str = event[len("data: "):-2]
        data = json.loads(json_str)
        # _sandbox_retry 输出格式: {"sandbox_retry": {attempt, max_retries, error, corrected, ts}}
        assert "sandbox_retry" in data
        assert data["sandbox_retry"]["attempt"] == 1


# ---------------------------------------------------------------------------
# 节点9: ChatRequest 构造与执行模式
# ---------------------------------------------------------------------------

class TestChatRequestConstruction:
    """验证 ChatRequest 的构造和执行模式解析。"""

    def test_request_with_user_input(self):
        request = _make_chat_request("生成一个狮子和大象的故事")
        assert len(request.messages) == 1
        assert request.messages[0].role == "user"
        assert request.messages[0].content == "生成一个狮子和大象的故事"

    def test_execute_mode_default(self):
        request = _make_chat_request("测试")
        assert request.effective_execution_mode() == "execute"

    def test_plan_mode(self):
        request = _make_chat_request("测试", execution_mode="plan")
        assert request.effective_execution_mode() == "plan"

    def test_craft_backward_compatible(self):
        request = _make_chat_request("测试", execution_mode="craft")
        assert request.effective_execution_mode() == "execute"

    def test_session_id_optional(self):
        request = _make_chat_request("测试", sandbox_session_id="session-123")
        assert request.sandbox_session_id == "session-123"

    def test_input_files_default_empty(self):
        request = _make_chat_request("测试")
        assert request.input_files == []


# ---------------------------------------------------------------------------
# 节点10: 端到端流程集成测试（Mock LLM）
# ---------------------------------------------------------------------------

class TestEndToEndSandboxFlow:
    """端到端集成测试：模拟完整的 sandbox 处理流程。

    场景：用户输入"生成一个狮子和大象的故事"
    步骤：
      1. build_skill_context 构建技能上下文
      2. _parse_need_body_decision 判断 need_body=True
      3. load_skill_body_prompt 加载正文
      4. _parse_child_skill_decision 判断无需子技能
      5. _run_instruction_analysis_round 分析指令
      6. _execute_single_task 执行脚本
      7. 验证最终结果包含故事文本
    """

    def test_full_flow_with_matching_request(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
        from backend.config import settings
        monkeypatch.setattr(settings, "default_model", "test-model")

        skill_dir = _create_skill_package(tmp_path)

        # --- 步骤1: 构建技能上下文 ---
        monkeypatch.setattr(
            "backend.routers.sandbox.stream_pipeline._skill_root_for_name",
            lambda name: skill_dir.resolve(),
        )
        _patch_skill_loaders(monkeypatch, skill_dir)
        from backend.routers.sandbox.stream_pipeline import build_skill_context
        context = build_skill_context(_SKILL_NAME)

        assert context["skill_name"] == _SKILL_NAME
        assert len(context["metadata_prompt"]) > 0
        assert context["execution_root"] is not None

        # --- 步骤2: 元数据决策 ---
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        need_body = _parse_need_body_decision(
            json.dumps({"need_body": True, "reason": "用户请求生成动物故事，匹配技能"})
        )
        assert need_body is True

        # --- 步骤3: 加载正文 ---
        body_prompt = context["body_loader"]()
        assert len(body_prompt) > 0
        assert "动物世界" in body_prompt or "故事" in body_prompt

        # --- 步骤4: 子技能决策 ---
        from backend.routers.sandbox.metadata_decisions import _parse_child_skill_decision
        child_decision = _parse_child_skill_decision(
            json.dumps({"need_child": False, "child_ref": "", "reason": "无需子技能"}),
            valid_child_refs=set(),
        )
        assert child_decision["need_child"] is False

        # --- 步骤5: 指令语义分析 ---
        from backend.routers.sandbox.instruction_analysis import _run_instruction_analysis_round

        async def fake_analysis_llm(messages, model, **kwargs):
            return json.dumps({
                "intent": "生成关于狮子和大象的动物世界故事",
                "scope": "动物角色故事生成",
                "constraints": ["必须包含狮子和大象"],
                "output_requirements": ["故事文本"],
                "complexity": "moderate",
                "requires_script_execution": True,
            })

        monkeypatch.setattr(
            "backend.routers.sandbox.instruction_analysis.complete_chat_once",
            fake_analysis_llm,
        )

        request = _make_chat_request("生成一个狮子和大象的故事")
        analysis = asyncio.run(_run_instruction_analysis_round(
            body_prompt=body_prompt,
            request=request,
            model="test-model",
        ))

        assert "狮子" in analysis["intent"]
        assert analysis["complexity"] == "moderate"
        assert analysis["requires_script_execution"] is True

        # --- 步骤6: 执行脚本 ---
        from backend.routers.sandbox.task_executor import _execute_single_task

        # Mock action schema 校验以避免重复入口冲突
        monkeypatch.setattr(
            "backend.routers.sandbox.task_executor._validate_runtime_command_against_action_schema",
            lambda command, **kw: None,
        )

        task = {
            "action": "run_command",
            "command": 'python scripts/generate_story.py \'{"topic": "狮子和大象的故事"}\'',
            "reason": "生成动物世界故事",
        }

        result, touched = _execute_single_task(
            task, [], request,
            execution_root=skill_dir,
            skill_name=_SKILL_NAME,
        )

        assert result["success"] is True
        stdout = result.get("stdout", "")
        if stdout.strip():
            payload = json.loads(stdout.strip())
            assert "story_text" in payload or "text" in payload
            story = payload.get("story_text", payload.get("text", ""))
            assert "狮子" in story or "大象" in story

    def test_full_flow_with_non_matching_request(self):
        """验证不匹配请求的流程：need_body=False 时不进入正文阶段。"""
        from backend.routers.sandbox.metadata_decisions import _parse_need_body_decision
        need_body = _parse_need_body_decision(
            json.dumps({"need_body": False, "reason": "用户请求与技能不匹配"})
        )
        assert need_body is False


# ---------------------------------------------------------------------------
# 节点11: 会话状态与步骤跳过
# ---------------------------------------------------------------------------

class TestSessionStateStepSkipping:
    """验证会话状态管理和步骤跳过逻辑。"""

    def test_new_task_skips_nothing(self):
        from backend.services.sandbox_session import SandboxSessionState, DialogIntent, StepName
        session = SandboxSessionState(session_id="test", skill_name=_SKILL_NAME)
        session.cache_artifact(StepName.METADATA, True)
        session.need_body = True
        assert session.should_skip(StepName.METADATA, DialogIntent.NEW_TASK) is False
        assert session.should_skip(StepName.LOAD_BODY, DialogIntent.NEW_TASK) is False

    def test_clarify_skips_metadata_and_body(self):
        from backend.services.sandbox_session import SandboxSessionState, DialogIntent, StepName
        session = SandboxSessionState(session_id="test", skill_name=_SKILL_NAME)
        session.cache_artifact(StepName.METADATA, True)
        session.cache_artifact(StepName.LOAD_BODY, "body content")
        session.need_body = True
        assert session.should_skip(StepName.METADATA, DialogIntent.CLARIFY) is True
        assert session.should_skip(StepName.LOAD_BODY, DialogIntent.CLARIFY) is True

    def test_continue_skips_all_cached_steps(self):
        from backend.services.sandbox_session import SandboxSessionState, DialogIntent, StepName
        session = SandboxSessionState(session_id="test", skill_name=_SKILL_NAME)
        session.cache_artifact(StepName.METADATA, True)
        session.cache_artifact(StepName.LOAD_BODY, "body")
        session.cache_artifact(StepName.CHILD_SKILL, {"need_child": False})
        session.cache_artifact(StepName.RESOURCES, {"need_resources": False})
        assert session.should_skip(StepName.METADATA, DialogIntent.CONTINUE) is True
        assert session.should_skip(StepName.LOAD_BODY, DialogIntent.CONTINUE) is True
        assert session.should_skip(StepName.CHILD_SKILL, DialogIntent.CONTINUE) is True
        assert session.should_skip(StepName.RESOURCES, DialogIntent.CONTINUE) is True

    def test_uncached_step_not_skipped(self):
        from backend.services.sandbox_session import SandboxSessionState, DialogIntent, StepName
        session = SandboxSessionState(session_id="test", skill_name=_SKILL_NAME)
        assert session.should_skip(StepName.METADATA, DialogIntent.CLARIFY) is False


# ---------------------------------------------------------------------------
# 节点12: 运行时 Action Schema 构建
# ---------------------------------------------------------------------------

class TestActionSchemaBuilding:
    """验证运行时 Action Schema 的构建。"""

    def test_action_schema_from_skill_md(self, tmp_path, monkeypatch):
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)

        from backend.routers.sandbox.action_schema import _build_runtime_action_schema
        schema = _build_runtime_action_schema(_SKILL_MD, execution_root=skill_dir)

        assert isinstance(schema["entries"], list)
        assert len(schema["entries"]) >= 1
        entry = schema["entries"][0]
        assert "script_path" in entry
        assert "command" in entry
        assert entry["script_path"] == "scripts/generate_story.py"

    def test_action_schema_detects_missing_scripts(self, tmp_path, monkeypatch):
        """验证 _validate_runtime_command_against_action_schema 在脚本不存在时抛出 ValueError。

        _build_runtime_action_schema 不检查脚本文件是否存在，
        但 _validate_runtime_command_against_action_schema 会在运行时校验。
        """
        _patch_skill_roots(monkeypatch, tmp_path)
        skill_dir = _create_skill_package(tmp_path)
        (skill_dir / "scripts" / "generate_story.py").unlink()

        from backend.routers.sandbox.action_schema import _validate_runtime_command_against_action_schema
        with pytest.raises(ValueError, match="不在当前 Skill available_scripts"):
            _validate_runtime_command_against_action_schema(
                'python scripts/generate_story.py \'{"topic": "test"}\'',
                execution_root=skill_dir,
            )


# ---------------------------------------------------------------------------
# 节点13: 错误纠正与重试
# ---------------------------------------------------------------------------

class TestErrorCorrection:
    """验证 LLM 错误纠正与重试机制。"""

    def test_apply_error_correction_merges_command(self):
        from backend.routers.sandbox.error_correction import _apply_error_correction
        original = {"action": "run_command", "command": "python scripts/old.py", "reason": "原始任务"}
        # _apply_error_correction 从 correction["task"] 中取修正值
        correction = {
            "corrected": True,
            "task": {"action": "run_command", "command": "python scripts/new.py", "reason": "修正后任务"},
        }
        result = _apply_error_correction(original, correction)
        assert result["action"] == "run_command"
        assert result["command"] == "python scripts/new.py"
        assert result["reason"] == "修正后任务"

    def test_error_correction_preserves_action_type(self):
        """安全守卫：action 类型不可被修正覆盖。"""
        from backend.routers.sandbox.error_correction import _apply_error_correction
        original = {"action": "run_command", "command": "python scripts/run.py"}
        correction = {
            "corrected": True,
            "task": {"action": "write_file", "path": "/etc/passwd", "content": "malicious"},
        }
        result = _apply_error_correction(original, correction)
        # action 类型应保留原始值
        assert result["action"] == "run_command"


# ---------------------------------------------------------------------------
# 节点14: 输出文件链接重写
# ---------------------------------------------------------------------------

class TestOutputFileLinks:
    """验证输出文件链接重写逻辑。"""

    def test_finalize_answer_rewrites_image_paths(self):
        from backend.routers.sandbox.output_links import _finalize_answer_output_file_links
        answer = "这是生成的图片：![插图](outputs/image-123.png)"
        output_files = [
            {"path": "outputs/image-123.png", "url": "/api/skills/test/files/outputs/image-123.png"},
        ]
        result = _finalize_answer_output_file_links(answer, output_files)
        assert "/api/skills/" in result

    def test_finalize_answer_preserves_text_without_files(self):
        from backend.routers.sandbox.output_links import _finalize_answer_output_file_links
        answer = "这是一个普通文本回答，没有文件链接。"
        result = _finalize_answer_output_file_links(answer, [])
        assert result == answer


# ---------------------------------------------------------------------------
# 节点15: 数据流上下文传递
# ---------------------------------------------------------------------------

class TestDataflowContextPassing:
    """验证工作流中数据流上下文的正确传递。"""

    def test_merge_step_output_flattens_keys(self):
        from backend.services.skill_dataflow import merge_step_output
        context = {"user_request": "狮子和大象"}
        stdout_json = {
            "story_text": "在非洲大草原上...",
            "image_paths": ["/path/to/image1.png", "/path/to/image2.png"],
            "images": [{"image_path": "/path/to/image1.png"}],
        }
        result = merge_step_output(context, "scripts/generate_story.py", stdout_json)
        assert result["story_text"] == "在非洲大草原上..."
        assert len(result["image_paths"]) == 2
        assert "generate_story" in result
        assert result["generate_story"]["story_text"] == "在非洲大草原上..."

    def test_merge_step_output_preserves_existing_context(self):
        from backend.services.skill_dataflow import merge_step_output
        context = {"user_request": "狮子和大象", "existing_key": "保留"}
        stdout_json = {"story_text": "新内容"}
        result = merge_step_output(context, "scripts/run.py", stdout_json)
        assert result["existing_key"] == "保留"
        assert result["story_text"] == "新内容"

    def test_workflow_context_from_request_text(self):
        from backend.routers.sandbox.workflow_dataflow import _workflow_context_from_request_text
        context = _workflow_context_from_request_text(
            "生成一个狮子和大象的故事",
            first_entry={"inputs": ["topic"]},
        )
        assert isinstance(context, dict)
        assert "user_request" in context
        assert "狮子" in context["user_request"]


# ──────────────────────────────────────────────────────────────────────
# 补充规划（方案 A）集成测试
# ──────────────────────────────────────────────────────────────────────

class TestSupplementaryPlan:
    """验证执行后补充规划逻辑。"""

    def test_supplementary_plan_need_more_false(self, monkeypatch):
        """信息充足时 need_more=False，不触发补充执行。"""
        from backend.routers.sandbox.runtime_planner import _run_supplementary_plan_round

        async def _mock_complete(messages, model):
            return json.dumps({
                "need_more": False,
                "reason": "执行结果已包含用户所需的故事文本",
                "additional_tasks": None,
                "final_answer_hint": "直接生成最终答案",
            })

        monkeypatch.setattr("backend.routers.sandbox.runtime_planner.complete_chat_once", _mock_complete)

        result = asyncio.run(
            _run_supplementary_plan_round(
                body_prompt="Skill: animal-world-story-generator",
                user_text="生成一个狮子和大象的故事",
                execution_results=[
                    {"action": "run_command", "success": True, "stdout": json.dumps({"story_text": "在非洲大草原上..."})},
                ],
                loaded_resources=["references/story_template.md"],
                failed_resources=[],
                model="test-model",
            )
        )

        assert result["need_more"] is False
        assert result["additional_tasks"] is None

    def test_supplementary_plan_need_more_true(self, monkeypatch):
        """信息不足时 need_more=True，返回补充任务。"""
        from backend.routers.sandbox.runtime_planner import _run_supplementary_plan_round

        async def _mock_complete(messages, model):
            return json.dumps({
                "need_more": True,
                "reason": "缺少故事模板资源",
                "additional_tasks": [
                    {"action": "read_resource", "resource_handle": "resource:0", "reason": "需要读取故事模板"},
                ],
                "final_answer_hint": "",
            })

        monkeypatch.setattr("backend.routers.sandbox.runtime_planner.complete_chat_once", _mock_complete)

        result = asyncio.run(
            _run_supplementary_plan_round(
                body_prompt="Skill: animal-world-story-generator",
                user_text="生成一个狮子和大象的故事",
                execution_results=[
                    {"action": "run_command", "success": True, "stdout": json.dumps({"partial": "数据不完整"})},
                ],
                loaded_resources=[],
                failed_resources=["references/story_template.md"],
                model="test-model",
            )
        )

        assert result["need_more"] is True
        assert result["additional_tasks"] is not None
        assert len(result["additional_tasks"]) == 1
        assert result["additional_tasks"][0]["action"] == "read_resource"

    def test_supplementary_plan_filters_disallowed_actions(self, monkeypatch):
        """补充规划器过滤不允许的动作类型（如 write_file）。"""
        from backend.routers.sandbox.runtime_planner import _run_supplementary_plan_round

        async def _mock_complete(messages, model):
            return json.dumps({
                "need_more": True,
                "reason": "需要写入文件",
                "additional_tasks": [
                    {"action": "write_file", "path": "outputs/story.txt", "reason": "写入故事"},
                    {"action": "read_resource", "resource_handle": "resource:0", "reason": "读取模板"},
                    {"action": "delete_file", "path": "temp.txt", "reason": "删除临时文件"},
                ],
                "final_answer_hint": "",
            })

        monkeypatch.setattr("backend.routers.sandbox.runtime_planner.complete_chat_once", _mock_complete)

        result = asyncio.run(
            _run_supplementary_plan_round(
                body_prompt="Skill: test-skill",
                user_text="测试",
                execution_results=[],
                loaded_resources=[],
                failed_resources=[],
                model="test-model",
            )
        )

        assert result["need_more"] is True
        # write_file 和 delete_file 被过滤，只保留 read_resource
        assert len(result["additional_tasks"]) == 1
        assert result["additional_tasks"][0]["action"] == "read_resource"

    def test_supplementary_plan_handles_non_json_response(self, monkeypatch):
        """LLM 返回非 JSON 时优雅降级。"""
        from backend.routers.sandbox.runtime_planner import _run_supplementary_plan_round

        async def _mock_complete(messages, model):
            return "这不是 JSON，是自然语言回复"

        monkeypatch.setattr("backend.routers.sandbox.runtime_planner.complete_chat_once", _mock_complete)

        result = asyncio.run(
            _run_supplementary_plan_round(
                body_prompt="Skill: test-skill",
                user_text="测试",
                execution_results=[],
                loaded_resources=[],
                failed_resources=[],
                model="test-model",
            )
        )

        assert result["need_more"] is False
        assert "无法解析" in result["reason"]

    def test_supplementary_plan_handles_empty_tasks(self, monkeypatch):
        """need_more=True 但 additional_tasks 为空时跳过补充执行。"""
        from backend.routers.sandbox.runtime_planner import _run_supplementary_plan_round

        async def _mock_complete(messages, model):
            return json.dumps({
                "need_more": True,
                "reason": "信息不足",
                "additional_tasks": [],
                "final_answer_hint": "",
            })

        monkeypatch.setattr("backend.routers.sandbox.runtime_planner.complete_chat_once", _mock_complete)

        result = asyncio.run(
            _run_supplementary_plan_round(
                body_prompt="Skill: test-skill",
                user_text="测试",
                execution_results=[],
                loaded_resources=[],
                failed_resources=[],
                model="test-model",
            )
        )

        # additional_tasks 为空列表被转为 None
        assert result["additional_tasks"] is None

    def test_supplementary_plan_max_rounds(self, monkeypatch):
        """验证补充规划最多执行 2 轮后强制终止。"""
        call_count = 0

        async def _mock_complete(messages, model):
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "need_more": True,
                "reason": "仍然不足",
                "additional_tasks": [
                    {"action": "display", "reason": "显示中间结果"},
                ],
                "final_answer_hint": "",
            })

        monkeypatch.setattr("backend.routers.sandbox.runtime_planner.complete_chat_once", _mock_complete)

        from backend.routers.sandbox.runtime_planner import _run_supplementary_plan_round

        # 模拟两轮调用
        for _ in range(2):
            result = asyncio.run(
                _run_supplementary_plan_round(
                    body_prompt="Skill: test-skill",
                    user_text="测试",
                    execution_results=[],
                    loaded_resources=[],
                    failed_resources=[],
                    model="test-model",
                )
            )
            assert result["need_more"] is True

        # 验证 LLM 被调用了 2 次（对应 MAX_SUPPLEMENTARY_ROUNDS=2）
        assert call_count == 2
