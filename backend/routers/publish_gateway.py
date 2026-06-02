"""OpenAI-compatible API gateway for published skill endpoints.

Exposes /published/v1/chat/completions and /published/v1/models
endpoints that external systems can call using standard OpenAI SDK.
No authentication required — routing is based on the model name in the request.
"""

import json
import time
import uuid
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.publish_auth import (
    check_rate_limit,
    log_request,
    get_active_published_models,
)
from ..services.publish_config import get_config_by_model_name, validate_skills_available
from ..services.publish_skill_injector import build_system_prompt, get_skill_descriptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/published/v1", tags=["publish-gateway"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint (no auth required)."""
    # Route by model name
    config = get_config_by_model_name(request.model)
    if not config:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{request.model}' not found or not active",
        )

    endpoint_id = config["endpoint_id"]
    model_name = config["name"]

    # Rate limit by endpoint
    if not check_rate_limit(endpoint_id):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Validate and filter enabled skills
    enabled_skills = validate_skills_available(config.get("enabled_skills", []))

    # Build system prompt from skills
    system_prompt = build_system_prompt(enabled_skills)

    # Compose messages: inject system prompt + user messages
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    for msg in request.messages:
        messages.append({"role": msg.role, "content": msg.content})

    # Determine backend model
    backend_model = settings.publish_default_model or settings.default_model

    log_request(endpoint_id, model_name)

    if request.stream:
        return StreamingResponse(
            _stream_response(messages, backend_model, model_name),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        content = await complete_chat_once(messages, backend_model)
        return _build_completion_response(content, model_name)


async def _stream_response(messages: list[dict], backend_model: str, model_name: str):
    """Generate SSE stream in OpenAI format."""
    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    async for chunk in stream_chat(messages, backend_model):
        data = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{
                "index": 0,
                "delta": {"content": chunk},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(data)}\n\n"

    # Final chunk with finish_reason
    final = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


def _build_completion_response(content: str, model_name: str) -> dict:
    """Build a standard OpenAI chat completion response."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
            },
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


@router.get("/models")
async def list_models():
    """List all published models (no auth required)."""
    active_configs = get_active_published_models()

    models = []
    for config in active_configs:
        descriptions = get_skill_descriptions(config.get("enabled_skills", []))
        models.append({
            "id": config["name"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": "skill-creator-factory",
            "description": descriptions,
        })

    return {
        "object": "list",
        "data": models,
    }


@router.get("/models/{model_id}")
async def get_model(model_id: str):
    """Get details of a specific published model (no auth required)."""
    config = get_config_by_model_name(model_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    descriptions = get_skill_descriptions(config.get("enabled_skills", []))
    return {
        "id": config["name"],
        "object": "model",
        "created": int(time.time()),
        "owned_by": "skill-creator-factory",
        "description": descriptions,
        "skills": config.get("enabled_skills", []),
    }
