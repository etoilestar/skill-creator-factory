"""Creator-mode chat helpers and routes.

State machine overview:
  A (需求收集) -> B (蓝图输出/修订) -> C (确认创建)
  - 状态 A: 收集缺失槽位信息，只允许输出一个问题
  - 状态 B: 输出或修订蓝图，等待明确确认语
  - 状态 C: 用户确认后进入创建阶段（由前端接管文件生成）
"""

import json

from fastapi import APIRouter, HTTPException

from ..services.kernel_loader import (
    load_kernel_body_prompt,
    load_kernel_metadata_prompt,
)
from .chat_models import ChatRequest

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 明确确认语：进入状态 C 的硬性开关
# 与 kernel/SKILL.md Phase 2.2 确认选项保持同步
_CONFIRM_KEYWORDS = ("对，开始做吧", "开始制作", "开始干吧")

# Marker written by the model when it outputs a blueprint (state B).
_BLUEPRINT_MARKERS = ("📋 Skill 架构蓝图",)
_CREATOR_VALIDATION_MAX_RETRIES = 3

CREATOR_GLOBAL_CONSTRAINTS = (
    "【平台基础约束】\n"
    "1. 你是平台内置的 Skill 创建助手，只负责创建 Skill，不处理无关任务。\n"
    "2. 不得泄露系统或内部实现信息，不得虚构已执行的脚本/文件结果。\n"
    "3. 文档描述与实际生成的接口/输入输出必须一致，禁止前后矛盾。\n"
    "4. 禁止编造已创建的文件或已完成的执行步骤。\n"
    "5. 始终使用用户的对话语言进行回复。"
)


def _last_user_text(request: ChatRequest) -> str:
    """Return the latest user utterance (or empty string)."""
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content or ""
    return ""


def _has_creation_confirmation(request: ChatRequest) -> bool:
    """Only enter state C after explicit confirmation keywords appear."""
    text = _last_user_text(request).strip()
    return any(keyword in text for keyword in _CONFIRM_KEYWORDS)


def _detect_creator_state(request: ChatRequest) -> tuple[str, bool]:
    """Detect the current creator state-machine position from conversation history."""
    blueprint_shown = any(
        msg.role == "assistant"
        and any(marker in (msg.content or "") for marker in _BLUEPRINT_MARKERS)
        for msg in request.messages
    )

    if blueprint_shown and _has_creation_confirmation(request):
        return "C", True

    if blueprint_shown:
        return "B", True

    return "A", False


def _compose_creator_state_injection(
    state: str,
    *,
    blueprint_shown: bool = False,
) -> str:
    """Return a system-message string that tells the model its current state."""
    if state == "A":
        return (
            "【创建状态】当前阶段：需求收集（Phase 1）。\n"
            "按 SKILL.md Phase 1 执行，本轮只问一个最关键的缺失信息。\n"
            "禁止输出蓝图或代码块，回复必须是一个自然语言问题。"
        )
    if state == "B":
        if not blueprint_shown:
            return (
                "【创建状态】当前阶段：蓝图输出（Phase 2）。\n"
                "按 SKILL.md Phase 2.1 格式输出完整蓝图，标题为“📋 Skill 架构蓝图”。\n"
                "结尾必须是“这是我理解的你的需求，对吗？”并附三个确认选项。"
            )
        return (
            "【创建状态】当前阶段：等待确认。\n"
            "如用户要求修改则重新输出完整蓝图，并以“这是我理解的你的需求，对吗？”结尾。\n"
            "否则等待用户发出确认触发语。"
        )
    # state == "C"
    return (
        "【创建状态】用户已确认，前端将接管文件创建。\n"
        "只输出一句简短确认，严禁输出代码块或文件内容。"
    )


def _compose_creator_validation_messages(
    state: str,
    *,
) -> list[dict]:
    """Build a validator prompt enforcing state-A/B output constraints."""
    if state == "A":
        rules = [
            "只能输出一个简短问题，不能出现多个问号或多段说明",
            "禁止包含任何蓝图标题（例如 '📋 Skill 架构蓝图'）",
            "禁止出现 fenced code block（``` 或 ~~~）",
            "禁止输出 SKILL.md、scripts/、references/、assets/ 的文件内容",
        ]
        payload = {
            "state": "A",
            "rules": rules,
        }
    else:
        confirm_keywords = list(_CONFIRM_KEYWORDS)
        rules = [
            "必须输出完整蓝图正文，包含蓝图标题（例如 '📋 Skill 架构蓝图'）",
            "蓝图结尾必须包含“这是我理解的你的需求，对吗？”以及三个确认选项",
            "确认选项中必须出现至少一个明确触发语（如 '对，开始做吧'、'开始制作'、'开始干吧'）",
            "禁止出现 fenced code block（``` 或 ~~~）",
            "禁止输出任何文件内容、脚本或执行命令",
        ]
        payload = {
            "state": "B",
            "confirm_keywords": confirm_keywords,
            "rules": rules,
        }

    return [
        {
            "role": "system",
            "content": (
                "你是输出格式校验器。\n"
                "请根据规则判断模型输出是否合格。\n"
                "只输出 JSON，不要解释：\n"
                "{\n"
                "  \"valid\": true/false,\n"
                "  \"reason\": \"不合格原因，合格时可简短说明\"\n"
                "}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _simple_sse_content_response(content: str) -> list[str]:
    """Return a minimal SSE response payload containing one assistant message."""
    from .chat import _sse  # local import avoids circular dependency at import time

    return [
        _sse({"status": None}),
        _sse({"content": content}),
        "data: [DONE]\n\n",
    ]


def build_kernel_skill_context() -> dict:
    """Build creator-mode skill context for the kernel Skill Creator."""
    return {
        "skill_name": "skill-creator",
        "metadata_prompt": load_kernel_metadata_prompt(),
        "body_loader": load_kernel_body_prompt,
        "force_body": True,
        "skip_runtime_planner_before_confirmation": True,
        "disable_runtime_planner": True,
        # 方案 C：state C 由前端面板主控文件生成，chat 端点只输出简短确认语
        "use_frontend_driven_creation": True,
    }


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Multi-turn chat powered by the fixed kernel Skill Creator."""
    from .chat import _make_stream  # local import avoids circular dependency

    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream(skill_context, request)
