"""Creator-mode chat helpers and routes."""

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..services.kernel_loader import load_kernel_creator_body_prompt
from .chat_models import ChatRequest

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Explicit confirmation keywords reused by sandbox_chat for gating flows.
_CONFIRM_KEYWORDS = ("对，开始做吧", "开始制作", "开始干吧")


def _last_user_text(request: ChatRequest) -> str:
    """Return the latest user utterance (or empty string)."""
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content or ""
    return ""


def _has_creation_confirmation(request: ChatRequest) -> bool:
    """Return True when the user confirms creation explicitly."""
    text = _last_user_text(request).strip()
    return any(keyword in text for keyword in _CONFIRM_KEYWORDS)


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
    from .chat import _make_stream_creator  # local import avoids circular dependency

    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream_creator(skill_context, request)
