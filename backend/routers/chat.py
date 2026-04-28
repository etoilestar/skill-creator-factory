import asyncio
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
from ..services import skill_executor

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


_OPEN_TAG = "<skill_action>"
_CLOSE_TAG = "</skill_action>"


def _safe_flush_len(text: str) -> int:
    """Return how many leading characters of *text* can safely be emitted.

    We must not emit a prefix that could be the start of a ``<skill_action>``
    tag that hasn't been fully received yet.
    """
    for i in range(1, len(_OPEN_TAG)):
        if text.endswith(_OPEN_TAG[:i]):
            return len(text) - i
    return len(text)


def _make_stream(system_prompt: str, request: ChatRequest):
    model = request.model or settings.default_model
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": m.role, "content": m.content} for m in request.messages]

    async def generate():
        try:
            # Buffer that accumulates text while we scan for skill_action tags.
            buf = ""

            async for chunk in stream_chat(messages, model):
                buf += chunk

                # Drain all complete skill_action tags from the front of buf.
                while True:
                    open_pos = buf.find(_OPEN_TAG)

                    if open_pos == -1:
                        # No open tag present — emit safe prefix and stop.
                        safe = _safe_flush_len(buf)
                        if safe > 0:
                            yield _sse({"content": buf[:safe]})
                            buf = buf[safe:]
                        break

                    # Emit text that precedes the open tag.
                    if open_pos > 0:
                        yield _sse({"content": buf[:open_pos]})
                        buf = buf[open_pos:]

                    close_pos = buf.find(_CLOSE_TAG)
                    if close_pos == -1:
                        # Tag not yet complete; wait for more chunks.
                        break

                    # Extract JSON between the tags.
                    json_str = buf[len(_OPEN_TAG):close_pos]
                    buf = buf[close_pos + len(_CLOSE_TAG):]

                    try:
                        action_data = json.loads(json_str)
                        result = await asyncio.to_thread(
                            skill_executor.run_action, action_data
                        )
                    except json.JSONDecodeError as exc:
                        result = {
                            "action": "unknown",
                            "name": "",
                            "success": False,
                            "message": f"JSON 解析错误: {exc}",
                            "path": None,
                        }

                    yield _sse({"action_result": result})
                    # Continue the while-loop to process any further tags.

            # Flush remaining buffer after the stream ends.
            if buf:
                yield _sse({"content": buf})

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
