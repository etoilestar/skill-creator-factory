import httpx
import json
import os
import logging
from collections.abc import AsyncGenerator

from ..config import settings


logger = logging.getLogger(__name__)


def _auth_headers() -> dict:
    """Return Authorization header when an OpenAI API key is configured."""
    api_key = (
        getattr(settings, "llm_api_key", None)
        or getattr(settings, "openai_api_key", None)
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )

    if api_key:
        return {"Authorization": f"Bearer {api_key}"}

    return {}


def _build_chat_completions_url(base_url: str) -> str:
    """Build OpenAI-compatible chat completions URL.

    Supported forms:
    - http://127.0.0.1:11434
    - http://127.0.0.1:11434/v1
    - http://127.0.0.1:11434/v1/chat/completions
    """
    base = base_url.rstrip("/")

    if base.endswith("/v1/chat/completions"):
        return base

    if base.endswith("/v1"):
        return f"{base}/chat/completions"

    return f"{base}/v1/chat/completions"


def _get_api_key() -> str:
    """Ollama ignores the key, but OpenAI-compatible services usually expect one."""
    return (
        getattr(settings, "llm_api_key", None)
        or getattr(settings, "openai_api_key", None)
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "ollama"
    )


def _build_payload(
    *,
    messages: list[dict],
    model: str,
    stream: bool,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    temperature = getattr(settings, "temperature", None)
    if temperature is not None:
        payload["temperature"] = temperature

    max_tokens = getattr(settings, "max_tokens", None)
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    return payload


def _build_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_api_key()}",
    }


async def complete_chat_once(messages: list[dict], model: str) -> str:
    """Non-streaming chat completion.

    用于 metadata 阶段的静默模型调用。
    """
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=False)
    headers = _build_headers()
    timeout = float(os.environ.get("LM_TIMEOUT_SECONDS", "6000"))

    logger.info("[LLM][once] request model=%s url=%s messages=%d", model, url, len(messages))

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    choices = data.get("choices") or []
    if not choices:
        logger.warning("[LLM][once] empty choices")
        return ""

    choice = choices[0]

    message = choice.get("message") or {}
    content = message.get("content")

    if not content:
        content = choice.get("text") or ""

    logger.info(
        "[LLM][once] response length=%d content=\n%s",
        len(content),
        content,
    )

    return content


async def stream_chat(messages: list[dict], model: str) -> AsyncGenerator[str, None]:
    """Stream chat completion from Ollama/OpenAI-compatible API."""
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=True)
    headers = _build_headers()
    timeout = float(os.environ.get("LM_TIMEOUT_SECONDS", "6000"))

    logger.info("[LLM][stream] request model=%s url=%s messages=%d", model, url, len(messages))

    full_content: list[str] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line:
                    continue

                if not line.startswith("data: "):
                    continue

                data_str = line[6:].strip()

                if data_str == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning("[LLM][stream] invalid json line=%s", data_str[:500])
                    continue

                choices = data.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]

                delta = choice.get("delta") or {}
                content = delta.get("content")

                if not content:
                    message = choice.get("message") or {}
                    content = message.get("content")

                if content:
                    full_content.append(content)
                    yield content

    final_text = "".join(full_content)

    logger.info(
        "[LLM][stream] response done length=%d content=\n%s",
        len(final_text),
        final_text,
    )


async def check_connection() -> dict:
    """Check if the LLM backend is reachable and return available models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.llm_base_url.rstrip('/')}/v1/models",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            models = [m["id"] for m in data.get("data", [])]
            return {"connected": True, "models": models}
    except Exception as exc:
        return {"connected": False, "error": str(exc), "models": []}