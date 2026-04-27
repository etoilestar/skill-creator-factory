import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..services.kernel_loader import load_kernel_system_prompt, load_skill_system_prompt
from ..services.llm_proxy import stream_chat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    model: Optional[str] = None


def _friendly_error(exc: Exception) -> str:
    """Convert LLM proxy exceptions to user-facing messages without leaking internals."""
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到 LLM 服务，请确认 Ollama/LM Studio 已启动"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"LLM 服务返回错误: HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "LLM 服务响应超时，请重试"
    return "生成时发生错误，请重试"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_stream(system_prompt: str, request: ChatRequest):
    model = request.model or settings.default_model
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": m.role, "content": m.content} for m in request.messages]

    async def generate():
        try:
            async for chunk in stream_chat(messages, model):
                yield _sse({"content": chunk})
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.exception("LLM stream error")
            yield _sse({"error": _friendly_error(exc)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Multi-turn chat powered by kernel/SKILL.md (skill-creator mode)."""
    try:
        system_prompt = load_kernel_system_prompt()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _make_stream(system_prompt, request)


@router.post("/sandbox/{skill_name}")
async def chat_in_sandbox(skill_name: str, request: ChatRequest):
    """Multi-turn chat with a specific skill loaded as system prompt (sandbox mode)."""
    try:
        system_prompt = load_skill_system_prompt(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _make_stream(system_prompt, request)
