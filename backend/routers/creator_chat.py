"""Creator-mode chat helpers and routes."""

import asyncio
import functools
import logging
import re
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import settings
from ..services.kernel_loader import load_kernel_creator_for_phase
from ..services.llm_proxy import stream_chat
from .chat_models import ChatRequest
from .chat_utils import (
    _blueprint_ready,
    _request_messages_with_files,
    _sse,
    _quick_actions,
    _thought,
)

logger = logging.getLogger(__name__)


def _safe_async_generator(generator_func):
    """
    安全的异步生成器包装器，正确处理 GeneratorExit 和 asyncio.CancelledError.
    
    解决问题："Attempted to exit cancel scope in a different task than it was entered in"
    """
    @functools.wraps(generator_func)
    async def wrapper(*args, **kwargs):
        try:
            async for item in generator_func(*args, **kwargs):
                try:
                    yield item
                except (GeneratorExit, asyncio.CancelledError):
                    # Client disconnected - clean exit
                    return
        except (GeneratorExit, asyncio.CancelledError):
            # Clean exit
            return
    return wrapper


router = APIRouter(prefix="/api/chat", tags=["chat"])

_BLUEPRINT_MARKER = "📋 Skill 架构蓝图"


def _guess_current_phase(messages: list) -> str:
    """Determine current creator phase from conversation history."""
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if role == "assistant" and _BLUEPRINT_MARKER in content:
            return "phase2"
    return "phase1"


def _parse_ask_user_question(text: str) -> tuple[str, list[dict] | None]:
    """
    Parse AskUserQuestion format to extract question and options, returns (clean_text, actions_list).
    
    Args:
        text: Raw text
        
    Returns:
        (clean_text, actions_list): actions_list is list of quick_actions buttons, or None if not found
    """
    # First try full ```text ... ``` format
    ask_user_pattern = re.compile(r'```text\s*问题[：:]\s*.*?```', re.DOTALL)
    ask_user_blocks = ask_user_pattern.findall(text)
    
    # If not found, try simpler format without ```text wrapper
    if not ask_user_blocks:
        simple_pattern = re.compile(r'(?:^|\n)(问题[：:].*?)(?=\n\n|\n\s*[A-Z][a-z]+[：:]|$)', re.DOTALL)
        ask_user_blocks = simple_pattern.findall(text)
    
    # If still not found, try looser detection
    if not ask_user_blocks:
        confirm_patterns = [
            r'(?:你对以上内容满意|准备进入下一步|确认|对吗|好吗)[？?]',
            r'(?:开始做|开始创建|继续实现)',
        ]
        has_confirm = any(re.search(p, text, re.IGNORECASE) for p in confirm_patterns)
        
        if has_confirm:
            actions = [
                {"text": "对，开始做吧", "value": "对，开始做吧", "style": "primary"},
                {"text": "大体对，但有些地方要改", "value": "大体对，但有些地方要改", "style": "default"},
                {"text": "不对，我重新说一下", "value": "不对，我重新说一下", "style": "default"},
            ]
            return text, actions
    
    if not ask_user_blocks:
        return text, None
    
    first_block = ask_user_blocks[0].strip()
    
    # Parse question - more flexible regex
    question_match = re.search(r'问题[：:]\s*["\']?(.*?)["\']?\s*(?:\n|$)', first_block, re.DOTALL)
    if not question_match:
        question_match = re.search(r'问题[：:]\s*(.*?)(?:\n|$)', first_block)
    
    # Extract options - support multiple formats
    options_matches = []
    
    options_with_quotes = re.findall(r'-\s*["\']?(.*?)["\']?\s*(?:\n|$)', first_block)
    if options_with_quotes:
        options_matches = options_with_quotes
    else:
        options_without_quotes = re.findall(r'-\s*(.+?)(?:\n|$)', first_block)
        if options_without_quotes:
            options_matches = options_without_quotes
    
    # If no options found but we have a confirm question, use default options
    if not options_matches and question_match:
        options_matches = ["对，开始做吧", "大体对，但有些地方要改", "不对，我重新说一下"]
    
    if not question_match:
        return text, None
    
    question = question_match.group(1).strip()
    actions = []
    
    for opt in options_matches:
        opt_text = opt.strip()
        if opt_text and not opt_text.lower().startswith(("选项", "options", "问题")):
            actions.append({
                "text": opt_text,
                "value": opt_text,
                "style": "default"
            })
    
    for action in actions:
        if any(keyword in action["text"] for keyword in ["开始做", "确认", "开始创建", "继续", "没问题", "对，"]):
            action["style"] = "primary"
    
    first_pos = text.find(first_block)
    if first_pos != -1:
        end_pos = first_pos + len(first_block)
        clean_text = text[:end_pos].rstrip()
    else:
        clean_text = text
    
    return clean_text, actions


def _ensure_single_question(text: str) -> tuple[str, list[dict] | None]:
    """Ensure we only show one set of quick actions at a time."""
    return _parse_ask_user_question(text)


@_safe_async_generator
async def _execute_conversation_mode(
    final_messages: list[dict],
    model: str,
    current_phase: str,
    request: ChatRequest,
) -> AsyncGenerator[str, None]:
    """Phase 1-2 conversation mode: stream directly to user."""
    yield _sse({"status": None})

    # Stream while collecting complete text
    assistant_chunks: list[str] = []
    
    try:
        async for chunk in stream_chat(final_messages, model):
            assistant_chunks.append(chunk)
            yield _sse({"content": chunk})
    except Exception:
        logger.exception("Error during LLM streaming")
    
    if not assistant_chunks:
        return
    
    assistant_text = "".join(assistant_chunks)
    
    # Check for quick actions after streaming completes
    if current_phase in ["phase1", "phase2", "unknown"]:
        _, actions = _ensure_single_question(assistant_text)
        if actions:
            yield _quick_actions(actions)
    if _BLUEPRINT_MARKER in assistant_text:
        yield _blueprint_ready()
    
    yield "data: [DONE]\n\n"


@_safe_async_generator
async def _make_stream_creator_generator(
    skill_context: dict,
    request: ChatRequest,
    model: str,
    parent_skill_name: str,
) -> AsyncGenerator[str, None]:
    """Core generator function that produces SSE stream."""
    
    current_phase = _guess_current_phase(getattr(request, "messages", []))

    phase_description = {
        "phase1": "Phase 1（深度需求挖掘）",
        "phase2": "Phase 2（技能架构蓝图）",
        "unknown": "阶段未知"
    }.get(current_phase, "阶段未知")

    yield _thought(
        "phase_detection",
        "阶段检测",
        f"当前处于 {phase_description}",
        {"current_phase": current_phase},
    )

    messages_list = getattr(request, "messages", [])
    is_first_time = len(messages_list) == 0
    
    if is_first_time:
        loading_phase = "first_time"
        prompt_desc = "初始指导（块 0+1）"
    elif current_phase == "phase1":
        loading_phase = "phase1"
        prompt_desc = "Phase1 指导（块 0+1）"
    elif current_phase == "phase2":
        loading_phase = "phase2"
        prompt_desc = "Phase2 指导（块 0+2）"
    else:
        loading_phase = "phase1"
        prompt_desc = "Phase1 指导（块 0+1）"
    
    yield _sse({"status": {"phase": "loading", "message": f"加载 {prompt_desc}…"}})
    body_prompt = load_kernel_creator_for_phase(loading_phase)
    prompt_type = f"渐进式披露 {prompt_desc}"

    yield _thought(
        "body_loaded",
        "加载 SKILL.md",
        f"{prompt_type}已加载，共 {len(body_prompt)} 字符",
        {"body_chars": len(body_prompt), "skill_name": parent_skill_name, "prompt_type": prompt_type},
    )

    if current_phase in ["phase1", "phase2", "unknown"]:
        single_step_instruction = """

---
## 交互指导

请严格按照 SKILL.md 中的流程执行:
- Phase 1: 通过多轮对话充分收集用户需求
- Phase 2: 生成完整蓝图并让用户确认

一次只问一个问题，等待用户回复。
"""
        body_prompt = body_prompt + single_step_instruction

    final_messages: list[dict] = [{"role": "system", "content": body_prompt}]
    final_messages.extend(_request_messages_with_files(request))

    async for sse in _execute_conversation_mode(
        final_messages=final_messages,
        model=model,
        current_phase=current_phase,
        request=request,
    ):
        yield sse


def _make_stream_creator(skill_context: dict, request: ChatRequest):
    """Creator-specific streaming for Phase 1-2 conversation flow."""
    model = request.model or settings.default_model
    parent_skill_name = skill_context.get("skill_name", "")

    return StreamingResponse(
        _make_stream_creator_generator(
            skill_context=skill_context,
            request=request,
            model=model,
            parent_skill_name=parent_skill_name,
        ),
        media_type="text/event-stream"
    )


def build_kernel_skill_context():
    """Build the context for the kernel skill mode."""
    return {
        "skill_name": "",
    }


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Create Skill Creator endpoint."""
    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream_creator(skill_context, request)