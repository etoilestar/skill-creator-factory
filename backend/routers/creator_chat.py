"""Creator-mode chat helpers and routes."""

import asyncio
import functools
import json
import logging
import re
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import settings
from ..services.kernel_loader import load_kernel_creator_body_prompt, load_kernel_creator_metadata_prompt, load_kernel_creator_for_phase, read_skill_resource_text
from ..services.llm_proxy import complete_chat_once, stream_chat
from .chat_models import ChatRequest
from .chat_utils import (
    _last_user_text,
    _allowed_skill_roots,
    _extract_input_session_dir,
    _find_created_skill_roots,
    _friendly_error,
    _is_within_sandbox,
    _planner_model_name,
    _request_messages_with_files,
    _sse,
    _quick_actions,
    _strip_markdown_json_fence,
    _thought,
    _validate_skill_md,
)
from .sandbox_chat import (
    _execute_single_task,
    _format_execution_report,
    _infer_skill_root_from_tasks,
    _plan_and_execute_generated_output,
    _run_skill_runtime_planner_round,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

_CREATOR_PHASE3_MARKER = '{"creator_phase":"phase3_start"}'


def _guess_current_phase(messages: list) -> str:
    """
    根据对话历史智能猜测当前所处的阶段。
    
    Returns:
        "phase1", "phase2", "phase3+", 或 "unknown"
    """
    # 首先检查是否已经有 phase3 marker
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if role == "assistant" and content and _CREATOR_PHASE3_MARKER in content:
            return "phase3+"

    # 如果没有 phase3 marker，尝试根据对话内容判断
    # 从最新的消息开始分析
    for msg in reversed(messages):
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if not content:
            continue

        # 检查是否有 Phase 2 的特征词
        phase2_keywords = ["蓝图", "架构", "I/O", "目录结构", "工作流", "确认"]
        if any(keyword in content for keyword in phase2_keywords):
            return "phase2"

    # 默认认为是 Phase 1
    return "phase1"


def _parse_ask_user_question(text: str) -> tuple[str, list[dict] | None]:
    """
    解析 AskUserQuestion 格式，提取问题和选项，返回 (clean_text, actions_list)。
    
    Args:
        text: 原始文本
        
    Returns:
        (clean_text, actions_list): actions_list 是 quick_actions 格式的按钮列表，如果没有则为 None
    """
    # 先尝试匹配完整的 ```text ... ``` 格式
    ask_user_pattern = re.compile(r'```text\s*问题:.*?```', re.DOTALL)
    ask_user_blocks = ask_user_pattern.findall(text)
    
    # 如果没找到，尝试匹配不完整的或不带 ```text 包裹的格式
    if not ask_user_blocks:
        # 尝试匹配 "问题:" 开头的段落
        simple_pattern = re.compile(r'(?:^|\n)(问题:.*?)(?=\n\n|\n[A-Z][a-z]+:|$)', re.DOTALL)
        ask_user_blocks = simple_pattern.findall(text)
    
    if not ask_user_blocks:
        return text, None
    
    # 只保留第一个 AskUserQuestion 块
    first_block = ask_user_blocks[0].strip()
    
    # 解析问题和选项
    # 格式通常是：
    # 问题: "xxx"
    # 选项:
    # - "xxx"
    # - "xxx"
    # 或者更简单的格式
    question_match = re.search(r'问题:\s*["\']?(.*?)["\']?\s*(?:\n|$)', first_block, re.DOTALL)
    if not question_match:
        # 尝试更宽松的匹配
        question_match = re.search(r'问题[:：]\s*(.*?)(?:\n|$)', first_block)
    
    # 提取选项 - 支持多种格式
    options_matches = []
    
    # 先尝试带引号的格式
    options_with_quotes = re.findall(r'-\s*["\']?(.*?)["\']?\s*(?:\n|$)', first_block)
    if options_with_quotes:
        options_matches = options_with_quotes
    else:
        # 尝试不带引号的格式
        options_without_quotes = re.findall(r'-\s*(.+?)(?:\n|$)', first_block)
        if options_without_quotes:
            options_matches = options_without_quotes
    
    if not question_match:
        return text, None
    
    question = question_match.group(1).strip()
    actions = []
    
    # 生成按钮列表
    for opt in options_matches:
        opt_text = opt.strip()
        if opt_text and not opt_text.startswith('选项') and not opt_text.startswith('options'):
            actions.append({
                "text": opt_text,
                "value": opt_text,
                "style": "default"
            })
    
    # 如果有确认类的关键词（如 "开始做吧"、"确认"），标记为 primary
    for action in actions:
        if any(keyword in action["text"] for keyword in ["开始做", "确认", "开始创建", "继续", "没问题"]):
            action["style"] = "primary"
    
    # 清理原始文本，只保留到第一个 AskUserQuestion 结束
    first_pos = text.find(first_block)
    if first_pos != -1:
        end_pos = first_pos + len(first_block)
        clean_text = text[:end_pos].rstrip()
    else:
        clean_text = text
    
    return clean_text, actions


def _ensure_single_question(text: str) -> tuple[str, list[dict] | None]:
    """
    解析 AskUserQuestion 格式，提取按钮，不清理太多内容。
    
    Args:
        text: 原始的模型输出文本
        
    Returns:
        原始文本（或处理后的文本），以及按钮列表
    """
    # 解析 AskUserQuestion 格式，提取按钮
    clean_text, actions = _parse_ask_user_question(text)

    # 如果没有解析到 AskUserQuestion，直接返回原始文本
    if not actions:
        return text, None

    # 有 AskUserQuestion，返回处理后的文本和按钮
    return clean_text, actions


async def _execute_conversation_mode(
    final_messages: list[dict],
    model: str,
    current_phase: str,
    request: ChatRequest,
    execution_root: Path,
    parent_skill_name: str,
) -> AsyncGenerator[str, None]:
    """Phase 1-2 对话模式：正常流式输出给用户。"""
    try:
        yield _sse({"status": None})
    except GeneratorExit:
        logger.info("Client disconnected at status clear")
        return

    # 直接实时流式输出给用户，同时收集完整文本
    assistant_chunks: list[str] = []
    
    try:
        async for chunk in stream_chat(final_messages, model):
            assistant_chunks.append(chunk)
            try:
                yield _sse({"content": chunk})
            except GeneratorExit:
                logger.info("Client disconnected during LLM streaming")
                return
    except GeneratorExit:
        logger.info("Client disconnected before/after LLM streaming")
        return
    except Exception:
        logger.exception("Error during LLM streaming")
    
    if not assistant_chunks:
        return
    
    assistant_text = "".join(assistant_chunks)
    
    # 流式输出完成后，检查是否有 quick_actions
    if current_phase in ["phase1", "phase2", "unknown"]:
        # 解析 AskUserQuestion 格式，提取按钮
        _, actions = _ensure_single_question(assistant_text)
        if actions:
            try:
                yield _quick_actions(actions)
            except GeneratorExit:
                logger.info("Client disconnected during quick_actions")
                return
    
    # 注意：Phase 1-2 模式下，即使检测到 phase3 marker，也不自动执行 Phase 3
    # 这是因为需要先让用户确认蓝图，由前端处理用户确认后的流程
    # Phase 3 的执行会在下一轮对话中（用户确认后），或者通过 _execute_phase3_mode 进行
    
    try:
        yield "data: [DONE]\n\n"
    except GeneratorExit:
        logger.info("Client disconnected at final [DONE]")
        return


async def _execute_phase3_mode(
    final_messages: list[dict],
    model: str,
    request: ChatRequest,
    execution_root: Path,
    parent_skill_name: str,
) -> AsyncGenerator[str, None]:
    """Phase 3+ 执行模式：收集模型输出，执行，给用户反馈进度。"""
    # 首先给用户发送进入执行模式的通知
    try:
        yield _sse({"type": "phase3_start", "message": "开始执行 Skill 创建流程..."})
    except GeneratorExit:
        logger.info("Client disconnected at phase3 start")
        return

    # 收集完整的模型输出，不直接流式给用户
    assistant_chunks: list[str] = []
    
    try:
        async for chunk in stream_chat(final_messages, model):
            assistant_chunks.append(chunk)
            # 这里不给用户流式输出，只收集
    except GeneratorExit:
        logger.info("Client disconnected during LLM streaming (phase3 mode)")
        return
    except Exception:
        logger.exception("Error during LLM streaming (phase3 mode)")
    
    if not assistant_chunks:
        try:
            yield _sse({"error": "未收到模型输出"})
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            logger.info("Client disconnected at error reporting")
        return
    
    assistant_text = "".join(assistant_chunks)
    
    # 验证是否有 phase3 marker
    if _CREATOR_PHASE3_MARKER not in assistant_text:
        try:
            yield _sse({"error": "未找到执行标记，返回对话模式"})
            # 还是把模型输出给用户看看
            yield _sse({"content": assistant_text})
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            logger.info("Client disconnected at error reporting")
        return

    # 开始执行，给用户进度反馈
    try:
        yield _sse({"type": "progress", "step": "解析执行计划...", "step_num": 1, "total_steps": 3})
    except GeneratorExit:
        logger.info("Client disconnected during progress 1")
        return

    try:
        # 执行计划
        exec_result = await _plan_and_execute_generated_output(
            assistant_text=assistant_text,
            request=request,
            model=model,
            require_confirmation=False,
            execution_root=execution_root,
            skill_name=parent_skill_name,
        )
        
        # 更新进度
        try:
            yield _sse({"type": "progress", "step": "执行完成，整理结果...", "step_num": 3, "total_steps": 3})
        except GeneratorExit:
            logger.info("Client disconnected during progress 3")
            return

        if exec_result.get("executed"):
            # 提取创建的文件信息
            output_files = exec_result.get("output_files") or []
            
            # 尝试从输出中推断 skill 名称和路径
            skill_name = None
            skill_path = None
            
            for file_info in output_files:
                file_path = file_info.get("path", "")
                if "skills/" in file_path and "/SKILL.md" in file_path:
                    # 从路径中提取 skill 名称
                    parts = file_path.split("skills/", 1)[1].split("/", 1)
                    if parts:
                        skill_name = parts[0]
                        skill_path = str(Path(file_path).parent)
                    break
            
            # 发送最终结果
            try:
                yield _sse({
                    "type": "completed",
                    "success": True,
                    "skill_name": skill_name,
                    "skill_path": skill_path,
                    "created_files": output_files,
                    "message": "Skill 创建成功！" if skill_name else "文件创建完成！"
                })
            except GeneratorExit:
                logger.info("Client disconnected during completion")
                return

    except GeneratorExit:
        logger.info("Client disconnected during execution")
        return
    except Exception as exc:
        logger.exception("creator phase3 execution failed")
        try:
            yield _sse({"type": "error", "message": f"执行失败：{_friendly_error(exc)}"})
        except GeneratorExit:
            logger.info("Client disconnected during error reporting")
            return
    
    try:
        yield "data: [DONE]\n\n"
    except GeneratorExit:
        logger.info("Client disconnected at final [DONE] (phase3)")
        return


def _build_creator_resource_catalog() -> list[dict]:
    """直接构建 creator 资源目录（不依赖从 prompt 中解析）。
    
    列出 kernel 目录下已知的参考资源，供资源选择器使用。
    """
    # 已知的 creator 参考资源
    known_resources = [
        # references/
        ("references/best-practices.md", "命名与输出最佳实践"),
        ("references/interaction-guide.md", "交互方式指南"),
        ("references/output-patterns.md", "输出格式示例"),
        ("references/workflows.md", "多步骤流程设计示例"),
        ("references/quick-actions-patterns.md", "快捷按钮设计示例"),
        # scripts/
        ("scripts/init-skill.py", "初始化 Skill 目录结构的脚本"),
    ]

    catalog: list[dict] = []
    for path, title in known_resources:
        kind = path.split("/", 1)[0]
        usage_hint = {
            "references": "设计/规范参考资料，按需阅读。",
            "assets": "模板或配置样例，按需阅读。",
            "scripts": "实现参考脚本，可读取其内容辅助生成。",
        }.get(kind, "按需阅读。")
        catalog.append(
            {
                "resource_handle": f"resource:{len(catalog)}",
                "kind": kind,
                "path": path,
                "title": title,
                "usage_hint": usage_hint,
            }
        )

    return catalog


def _extract_creator_resource_catalog(body_prompt: str) -> list[dict]:
    """Extract creator resources from prompt references without sandbox coupling.
    
    保留这个函数作为兼容性回退，但优先使用 _build_creator_resource_catalog()。
    """
    catalog: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"`(?P<path>(references|assets|scripts)/[^`]+)`(?P<title>：[^\n]+)?",
        re.M,
    )

    for match in pattern.finditer(body_prompt):
        path = match.group("path").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        kind = path.split("/", 1)[0]
        title = (match.group("title") or "").lstrip("：").strip()
        usage_hint = {
            "references": "设计/规范参考资料，按需阅读。",
            "assets": "模板或配置样例，按需阅读。",
            "scripts": "实现参考脚本，可读取其内容辅助生成。",
        }.get(kind, "按需阅读。")
        catalog.append(
            {
                "resource_handle": f"resource:{len(catalog)}",
                "kind": kind,
                "path": path,
                "title": title,
                "usage_hint": usage_hint,
            }
        )

    return catalog


def _creator_resource_catalog_for_selector(catalog: list[dict]) -> list[dict]:
    return [
        {
            "resource_handle": item["resource_handle"],
            "kind": item["kind"],
            "title": item.get("title", ""),
            "usage_hint": item.get("usage_hint", ""),
        }
        for item in catalog
    ]


def _compose_creator_resource_selection_prompt(current_phase_hint: str) -> str:
    """
    生成资源选择器的 system prompt。
    
    Args:
        current_phase_hint: 阶段提示，可以是 "phase1", "phase2", "phase3+", 或 "unknown"
    """
    # 不管什么阶段，都只做资源选择，输出严格 JSON 格式
    return (
        "你是 Creator 模式的资源按需加载助手。\n\n"
        "输入包含 Loaded SKILL.md、resource_catalog 和用户请求。\n"
        "目标：判断是否需要先读取部分资源帮助当前回答。\n\n"
        "规则：\n"
        "1. 仅能从 resource_catalog 中选择 resource_handle。\n"
        "2. 最多选择 5 个资源。\n"
        "3. 必须输出严格的 JSON 格式，不要其他说明。\n"
        "4. 不要输出自然语言对话，只输出 JSON。\n\n"
        "输出格式：\n"
        "{\n"
        '  "need_resources": true/false,\n'
        '  "resource_handles": ["resource:0", "resource:1"],\n'
        '  "reason": "简短原因"\n'
        "}\n"
    )


def _parse_creator_resource_selection_decision(
        text: str,
        *,
        resource_catalog: list[dict],
) -> dict:
    valid_handles = {
        str(item.get("resource_handle", "")).strip()
        for item in resource_catalog
        if str(item.get("resource_handle", "")).strip()
    }

    def _filter_handles(candidates: list[str]) -> list[str]:
        selected: list[str] = []
        for item in candidates:
            handle = str(item or "").strip()
            if not handle or handle not in valid_handles or handle in selected:
                continue
            selected.append(handle)
            if len(selected) >= 5:
                break
        return selected

    stripped = _strip_markdown_json_fence(text)
    parsed = None
    try:
        parsed = json.loads(stripped)
    except Exception:
        parsed = None

    # 判断是否为结构化JSON响应
    is_structured = isinstance(parsed, dict)

    if isinstance(parsed, dict):
        raw_need = parsed.get("need_resources")
        raw_handles = parsed.get("resource_handles", [])
        selected = _filter_handles(raw_handles if isinstance(raw_handles, list) else [])
        if isinstance(raw_need, str):
            need_resources = raw_need.strip().lower() in {"true", "1", "yes", "y"}
        elif raw_need is None:
            need_resources = bool(selected)
        else:
            need_resources = bool(raw_need)
        if not need_resources or not selected:
            return {
                "need_resources": False,
                "resource_handles": [],
                "reason": str(parsed.get("reason") or "").strip(),
                "original_text": text,
                "is_structured": is_structured,
            }
        return {
            "need_resources": True,
            "resource_handles": selected,
            "reason": str(parsed.get("reason") or "").strip(),
            "original_text": text,
            "is_structured": is_structured,
        }

    extracted = _filter_handles(re.findall(r"resource:\d+", text or ""))
    if extracted:
        return {
            "need_resources": True,
            "resource_handles": extracted,
            "reason": "从自由文本解析到资源句柄",
            "original_text": text,
            "is_structured": is_structured,
        }
    return {
        "need_resources": False,
        "resource_handles": [],
        "reason": "未选择资源",
        "original_text": text,
        "is_structured": is_structured,
    }


async def _run_creator_resource_selection_round(
        *,
        body_prompt: str,
        request: ChatRequest,
        model: str,
        resource_catalog: list[dict],
        current_phase_hint: str,
) -> dict:
    if not resource_catalog:
        return {"need_resources": False, "resource_handles": [], "reason": "无可用资源"}

    messages = [
        {"role": "system", "content": _compose_creator_resource_selection_prompt(current_phase_hint)},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "resource_catalog": _creator_resource_catalog_for_selector(resource_catalog),
                    "user_messages": _request_messages_with_files(request),
                    "last_user_text": _last_user_text(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    try:
        # 调用选择器模型

        decision_text = await complete_chat_once(messages, _planner_model_name(model))
        # print("planner decision_text:", decision_text)
    except Exception:
        logger.exception("creator resource selection round failed")
        return {"need_resources": False, "resource_handles": [], "reason": "选择器调用失败"}

    return _parse_creator_resource_selection_decision(
        decision_text,
        resource_catalog=resource_catalog,
    )


def _compose_creator_loaded_resources_prompt(
        *,
        skill_name: str,
        resource_catalog: list[dict],
        selected_handles: list[str],
) -> str:
    resource_by_handle = {
        str(item.get("resource_handle")): item for item in resource_catalog if item.get("resource_handle")
    }
    sections: list[str] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(str(handle))
        if not resource:
            continue
        path = str(resource.get("path") or "").strip()
        if not path:
            continue
        try:
            observation = read_skill_resource_text(
                skill_name,
                path,
                max_chars=settings.skill_resource_max_chars,
            )
        except Exception as exc:
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error: {exc}\n"
            )
            continue

        content = observation.get("content", "")
        truncated = observation.get("truncated", False)
        sections.append(
            f"### {handle}\n"
            f"- kind: {resource.get('kind')}\n"
            f"- path: `{path}`\n"
            f"- truncated: {truncated}\n\n"
            "```text\n"
            f"{content}\n"
            "```"
        )

    if not sections:
        return ""

    return (
            "\n\n---\n\n"
            "## Loaded On-Demand Resources (Creator)\n\n"
            "以下内容由宿主按需读取，可作为当前 Creator 回答上下文。\n\n"
            + "\n\n".join(sections)
    )


def build_kernel_skill_context() -> dict:
    """Build creator-mode skill context for the kernel Skill Creator."""
    return {
        "skill_name": "skill-creator",
        "body_loader": load_kernel_creator_body_prompt,
        "force_body": True,
        "enable_action_execution": True,
        "require_action_confirmation": False,
        "execution_root": settings.skills_path,
        "strict_skill_execution": True,
        "enable_resource_preload": True,
    }


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Multi-turn chat powered by the fixed kernel Skill Creator."""
    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream_creator(skill_context, request)


def _make_stream_creator(skill_context: dict, request: ChatRequest):
    """Creator-specific streaming with phase-aware behaviour.

    Unlike _make_stream which always runs the runtime planner, this function
    detects whether the conversation is still in Phase 1-2 (pure conversation)
    or has advanced to Phase 3+ (action execution).

    Phase detection:
    - If the LLM's response in the current turn contains the phase-3 marker,
      run the planner/executor on the generated output.
    - Otherwise, stream the LLM response directly without planning.

    This avoids the overhead and potential mis-planning of running the runtime
    planner on conversational Phase 1-2 turns that are purely asking questions.
    """
    model = request.model or settings.default_model
    _MAX_CMD_DISPLAY_LENGTH = 60
    execution_root = skill_context.get("execution_root")
    parent_skill_name = skill_context.get("skill_name", "")
    enable_resource_preload = bool(skill_context.get("enable_resource_preload", False))

    if execution_root is not None:
        execution_root = Path(execution_root).resolve()
        allowed_roots = _allowed_skill_roots()
        if not any(_is_within_sandbox(execution_root, r.resolve()) for r in allowed_roots):
            raise ValueError(
                f"execution_root '{execution_root}' is outside all allowed skill roots."
            )

    def _conversation_has_phase3(messages: list) -> bool:
        """Check whether a prior assistant message already contains the phase-3 marker."""
        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role == "assistant" and content and _CREATOR_PHASE3_MARKER in content:
                return True
        return False

    async def generate():
        """
        生成kernel Skill Creator的响应

        :return:
        """
        try:
            # ── Step 1: 先智能判断当前对话阶段 ──────────────
            current_phase = _guess_current_phase(getattr(request, "messages", []))

            phase_description = {
                "phase1": "Phase 1（深度需求挖掘）",
                "phase2": "Phase 2（技能架构蓝图）",
                "phase3+": "Phase 3+（工程化实现）",
                "unknown": "阶段未知"
            }.get(current_phase, "阶段未知")

            try:
                yield _thought(
                    "phase_detection",
                    "阶段检测",
                    f"当前处于 {phase_description}",
                    {"current_phase": current_phase},
                )
            except GeneratorExit:
                logger.info("Client disconnected at phase detection")
                return

            # ── Step 2: 根据阶段和对话历史加载合适的 SKILL.md 内容 ──────
            # 使用渐进式披露：只加载当前阶段需要的块
            # - 首次进入：first_time (block 0 + 1)
            # - Phase1: phase1 (block 0 + 1)
            # - Phase2: phase2 (block 0 + 2)
            # - Phase3+: phase3+ (blocks 0 + 3-6)
            messages_list = getattr(request, "messages", [])
            is_first_time = len(messages_list) == 0
            
            # Determine the loading phase
            if is_first_time:
                loading_phase = "first_time"
                prompt_desc = "初始指导（块 0+1）"
            elif current_phase == "phase1":
                loading_phase = "phase1"
                prompt_desc = "Phase1 指导（块 0+1）"
            elif current_phase == "phase2":
                loading_phase = "phase2"
                prompt_desc = "Phase2 指导（块 0+2）"
            elif current_phase == "phase3+":
                loading_phase = "phase3+"
                prompt_desc = "Phase3+ 指导（块 0+3-6）"
            else:
                loading_phase = "phase1"
                prompt_desc = "Phase1 指导（块 0+1）"
            
            try:
                yield _sse({"status": {"phase": "loading", "message": f"加载 {prompt_desc}…"}})
                body_prompt = load_kernel_creator_for_phase(loading_phase)
                prompt_type = f"渐进式披露 {prompt_desc}"

                yield _thought(
                    "body_loaded",
                    "加载 SKILL.md",
                    f"{prompt_type}已加载，共 {len(body_prompt)} 字符",
                    {"body_chars": len(body_prompt), "skill_name": parent_skill_name, "prompt_type": prompt_type},
                )
            except GeneratorExit:
                logger.info("Client disconnected during body loading")
                return

            # ── Step 3: 优化资源加载（Phase 1-2 才需要）─────────────────
            loaded_resources_prompt = ""
            if enable_resource_preload and current_phase in ["phase1", "phase2", "unknown"]:
                # 只在 Phase 1-2 时运行资源选择器
                # Phase 3+ 时跳过，直接进入 planner
                resource_catalog = _build_creator_resource_catalog()
                if resource_catalog:
                    try:
                        yield _sse({"status": {"phase": "loading_resources", "message": "按需加载资源…"}})
                    except GeneratorExit:
                        logger.info("Client disconnected at resource loading status")
                        return
                    
                    try:
                        resource_decision = await _run_creator_resource_selection_round(
                            body_prompt=body_prompt,
                            request=request,
                            model=model,
                            resource_catalog=resource_catalog,
                            current_phase_hint=current_phase,
                        )
                    except (GeneratorExit, asyncio.CancelledError):
                        logger.info("Client disconnected during resource selection")
                        return

                    # 只处理资源选择，不处理自然语言对话
                    is_structured = resource_decision.get("is_structured", False)

                    if is_structured:
                        if resource_decision.get("need_resources"):
                            # 需要加载资源的情况
                            try:
                                yield _thought(
                                    "resource_selection",
                                    "资源选择",
                                    (
                                        f"加载 {len(resource_decision.get('resource_handles', []))} 个资源"
                                        if resource_decision.get("need_resources")
                                        else f"无需加载额外资源：{resource_decision.get('reason', '')}"
                                    ),
                                    {
                                        "need_resources": resource_decision.get("need_resources"),
                                        "resource_handles": resource_decision.get("resource_handles", []),
                                        "catalog_size": len(resource_catalog),
                                        "current_phase": current_phase,
                                    },
                                )
                            except GeneratorExit:
                                logger.info("Client disconnected during resource selection thought")
                                return

                            selected = resource_decision.get("resource_handles") or []
                            if selected:
                                loaded_resources_prompt = _compose_creator_loaded_resources_prompt(
                                    skill_name=parent_skill_name,
                                    resource_catalog=resource_catalog,
                                    selected_handles=selected,
                                )

            # 应用加载的资源到 body_prompt
            if loaded_resources_prompt:
                body_prompt = body_prompt + loaded_resources_prompt

            # Phase 1 和 Phase 2：添加轻量级指导
            if current_phase in ["phase1", "phase2", "unknown"]:
                single_step_instruction = """

---
## 交互指导

请严格按照 SKILL.md 中的流程执行：
- Phase 1：通过多轮对话充分收集用户需求
- Phase 2：生成完整蓝图并让用户确认
- Phase 3：执行实现（需要先输出 phase3 marker）

一次只问一个问题，等待用户回复。
"""
                body_prompt = body_prompt + single_step_instruction

            # ── Step 4: Build messages for the LLM ────────────────────────
            final_messages: list[dict] = [{"role": "system", "content": body_prompt}]
            final_messages.extend(_request_messages_with_files(request))

            if current_phase == "phase3+":
                # ── Phase 3+ path: run planner then execute ───────────────
                yield _sse({"status": {"phase": "planning", "message": "规划执行方案…"}})
                try:
                    runtime_plan = await _run_skill_runtime_planner_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        execution_root=execution_root,
                    )

                    mode = runtime_plan.get("mode")
                    tasks = runtime_plan.get("tasks") or []

                    yield _thought(
                        "planner_output",
                        "规划结果",
                        f"模式：{mode}，共 {len(tasks)} 个动作",
                        {
                            "mode": mode,
                            "task_count": len(tasks),
                            "tasks": [
                                {
                                    "action": t.get("action"),
                                    "command": (str(t.get("command") or ""))[:120] or None,
                                    "path": t.get("path") or t.get("resource_handle") or None,
                                    "reason": str(t.get("reason") or "")[:200],
                                }
                                for t in tasks
                            ],
                        },
                    )

                    if mode == "execute" and tasks:
                        _exec_inferred_root = _infer_skill_root_from_tasks(
                            runtime_plan, execution_root=execution_root
                        )
                        _exec_cwd = execution_root or _exec_inferred_root
                        _exec_session_dir = _extract_input_session_dir(
                            getattr(request, "input_files", []) or [], _exec_cwd
                        )

                        _exec_all_results: list[dict] = []
                        _exec_all_touched: list[Path] = []

                        for task in tasks:
                            task_action = str(task.get("action") or "").strip()

                            try:
                                if task_action == "run_command":
                                    cmd = str(task.get("command") or "")
                                    short_cmd = cmd[:_MAX_CMD_DISPLAY_LENGTH] + (
                                        "…" if len(cmd) > _MAX_CMD_DISPLAY_LENGTH else ""
                                    )
                                    yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
                                    yield _thought("action_start", "执行命令", short_cmd,
                                                   {"action": "run_command", "command": cmd[:200]})
                                elif task_action == "write_file":
                                    wf_path = str(task.get("path") or "")
                                    yield _sse({"status": {"phase": "writing", "message": f"写入文件：{wf_path}"}})
                                    yield _thought("action_start", "写入文件", wf_path,
                                                   {"action": "write_file", "path": wf_path})
                                elif task_action == "create_directory":
                                    cd_path = str(task.get("path") or "")
                                    yield _sse({"status": {"phase": "creating", "message": f"创建目录：{cd_path}"}})
                                    yield _thought("action_start", "创建目录", cd_path,
                                                   {"action": "create_directory", "path": cd_path})
                                elif task_action == "read_resource":
                                    res_path = str(task.get("path") or task.get("resource_handle") or "")
                                    yield _sse({"status": {"phase": "reading", "message": f"读取资源：{res_path}"}})
                                    yield _thought("action_start", "读取资源", res_path,
                                                   {"action": "read_resource", "path": res_path})
                                else:
                                    yield _thought("action_start", "执行动作", task_action, {"action": task_action})
                            except GeneratorExit:
                                logger.info("Client disconnected during task start")
                                return

                            task_result, task_touched = await asyncio.to_thread(
                                functools.partial(
                                    _execute_single_task,
                                    task,
                                    [],
                                    request,
                                    execution_root=execution_root,
                                    inferred_skill_root=_exec_inferred_root,
                                    skill_name=parent_skill_name,
                                    session_input_dir=_exec_session_dir,
                                )
                            )
                            _exec_all_results.append(task_result)
                            _exec_all_touched.extend(task_touched)

                            _safe_result = {
                                k: (v[:1000] if isinstance(v, str) else v)
                                for k, v in task_result.items()
                                if k not in {"content"}
                            }
                            success_flag = task_result.get("success", True)
                            try:
                                yield _thought(
                                    "action_result",
                                    "操作结果",
                                    f"{'成功' if success_flag else '失败'}",
                                    _safe_result,
                                )
                            except GeneratorExit:
                                logger.info("Client disconnected during task result")
                                return

                        # Validate any newly created Skill roots.
                        for root in _find_created_skill_roots(_exec_all_touched):
                            skill_md = root / "SKILL.md"
                            if skill_md.exists():
                                _validate_skill_md(skill_md)

                        _exec_all_output_files: list[dict] = []
                        for r in _exec_all_results:
                            _exec_all_output_files.extend(r.get("output_files") or [])

                        exec_result = {
                            "executed": True,
                            "reason": "已根据结构化 action plan 逐任务执行。",
                            "plan": runtime_plan,
                            "results": _exec_all_results,
                            "logs": [],
                            "output_files": _exec_all_output_files,
                        }

                        yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
                        from .sandbox_chat import _compose_final_answer_prompt

                        _final_messages = [
                            {"role": "system", "content": _compose_final_answer_prompt()},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "loaded_skill_prompt": body_prompt,
                                        "user_messages": _request_messages_with_files(request),
                                        "plan": runtime_plan,
                                        "execution_result": exec_result,
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ]

                        try:
                            yield _sse({"status": None})
                        except GeneratorExit:
                            logger.info("Client disconnected at status clear")
                            return
                        
                        if _exec_all_output_files:
                            try:
                                yield _sse({
                                    "action_result": {
                                        "action": "output_files",
                                        "name": parent_skill_name,
                                        "success": True,
                                        "message": f"生成了 {len(_exec_all_output_files)} 个文件",
                                        "output_files": _exec_all_output_files,
                                    }
                                })
                            except GeneratorExit:
                                logger.info("Client disconnected at output files")
                                return
                        
                        try:
                            async for chunk in stream_chat(_final_messages, model):
                                try:
                                    yield _sse({"content": chunk})
                                except GeneratorExit:
                                    logger.info("Client disconnected during final answer streaming")
                                    return
                        except GeneratorExit:
                            logger.info("Client disconnected before/after final answer streaming")
                            return
                        
                        try:
                            yield "data: [DONE]\n\n"
                        except GeneratorExit:
                            logger.info("Client disconnected at final [DONE]")
                            return
                        return

                    if mode == "ask_user":
                        try:
                            yield _sse({"status": None})
                        except GeneratorExit:
                            logger.info("Client disconnected at status clear")
                            return
                        missing = runtime_plan.get("missing") or []
                        text = "缺少必要信息，无法执行：\n" + "\n".join(
                            f"- {item}" for item in missing) if missing else "缺少必要信息。"
                        try:
                            yield _sse({"content": text})
                        except GeneratorExit:
                            logger.info("Client disconnected at ask_user content")
                            return
                        try:
                            yield "data: [DONE]\n\n"
                        except GeneratorExit:
                            logger.info("Client disconnected at final [DONE]")
                            return
                        return

                    # mode == direct_answer or not_applicable → fall through to LLM
                    try:
                        yield _sse({"status": None})
                    except GeneratorExit:
                        logger.info("Client disconnected at status clear")
                        return

                except GeneratorExit:
                    logger.info("Client disconnected during runtime planning/execution")
                    return
                except Exception as exc:
                    logger.exception("creator runtime planning/execution failed")
                    try:
                        yield _sse({"status": None})
                        yield _sse({"error": "错误：运行时规划或执行失败"})
                        yield "data: [DONE]\n\n"
                    except GeneratorExit:
                        logger.info("Client disconnected during error reporting")
                        return
                    return

            # ── 判断当前模式：对话模式还是执行模式 ──
            if current_phase in ["phase3+", "phase3", "phase4", "phase5"] or _CREATOR_PHASE3_MARKER in [m.content for m in request.messages]:
                # Phase 3+ 执行模式
                async for sse in _execute_phase3_mode(
                    final_messages=final_messages,
                    model=model,
                    request=request,
                    execution_root=execution_root,
                    parent_skill_name=parent_skill_name,
                ):
                    yield sse
            else:
                # Phase 1-2 对话模式
                async for sse in _execute_conversation_mode(
                    final_messages=final_messages,
                    model=model,
                    current_phase=current_phase,
                    request=request,
                    execution_root=execution_root,
                    parent_skill_name=parent_skill_name,
                ):
                    yield sse

        except GeneratorExit:
            # Client disconnected - exit gracefully, don't yield anything
            logger.info("Client disconnected during streaming (main handler)")
            return
        except Exception as exc:
            logger.exception("creator LLM stream error")
            try:
                yield _sse({"status": None})
                yield _sse({"error": _friendly_error(exc)})
                yield "data: [DONE]\n\n"
            except GeneratorExit:
                logger.info("Client disconnected during error reporting")
                return

    return StreamingResponse(generate(), media_type="text/event-stream")
