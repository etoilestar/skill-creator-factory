import asyncio
import time

import httpx
import json
import os
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from ..config import settings
from .model_router import _models_match


logger = logging.getLogger(__name__)


def _resolve_api_key() -> str | None:
    """Resolve the LLM API key from config or environment, returning None if absent."""
    return (
        settings.llm_api_key
        or settings.openai_api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def _resolve_image_api_key() -> str | None:
    """Resolve the image-generation API key independently from the LLM key."""
    return (
        settings.image_api_key
        or os.environ.get("IMAGE_API_KEY")
        or _resolve_api_key()
    )


def _auth_headers() -> dict:
    """Return Authorization header when an OpenAI API key is configured."""
    api_key = _resolve_api_key()
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

def _build_image_generations_url(base_url: str) -> str:
    """
    Build OpenAI-compatible image generations URL.

    Supported forms:
    - http://127.0.0.1:11435
    - http://127.0.0.1:11435/v1
    - http://127.0.0.1:11435/v1/images/generations
    """
    base = base_url.rstrip("/")

    if base.endswith("/v1/images/generations"):
        return base

    if base.endswith("/v1"):
        return f"{base}/images/generations"

    return f"{base}/v1/images/generations"

def _get_api_key() -> str:
    """Ollama ignores the key, but OpenAI-compatible services usually expect one."""
    return _resolve_api_key() or "ollama"


def _build_payload(
    *,
    messages: list[dict],
    model: str,
    stream: bool,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    effective_temperature = temperature if temperature is not None else settings.temperature
    if effective_temperature is not None:
        payload["temperature"] = effective_temperature

    effective_max_tokens = max_tokens if max_tokens is not None else settings.max_tokens
    if effective_max_tokens is not None:
        payload["max_tokens"] = effective_max_tokens

    return payload


def _ack_response_model(*, expected_model: str, actual_model: str | None, phase: str) -> None:
    """Log and optionally enforce provider model acknowledgement."""
    matched = _models_match(expected_model, actual_model) if actual_model else None
    logger.info(
        "[LLM][ack] phase=%s expected_model=%s actual_model=%s matched=%s",
        phase,
        expected_model,
        actual_model or "",
        matched,
    )
    if settings.model_ack_strict and actual_model and not matched:
        raise ValueError(
            f"LLM provider returned model {actual_model!r}, expected {expected_model!r}"
        )


def _build_headers(api_key: str | None = None) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or _get_api_key()}",
    }


def _build_image_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_resolve_image_api_key() or 'ollama'}",
    }


async def complete_chat_once(
    messages: list[dict], model: str, *, base_url: str | None = None,
    api_key: str | None = None, max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Non-streaming chat completion.

    用于 metadata 阶段的静默模型调用。
    """
    url = _build_chat_completions_url(base_url or settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=False, max_tokens=max_tokens,
                             temperature=temperature)
    headers = _build_headers(api_key)
    timeout = float(settings.llm_timeout_seconds)

    logger.info("[LLM][once] request model=%s url=%s messages=%d", model, url, len(messages))

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    _ack_response_model(expected_model=model, actual_model=data.get("model"), phase="once")

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

async def generate_image_once(
    *,
    prompt: str,
    model: str,
    size: str | None = None,
    response_format: str = "b64_json",
) -> dict:
    """Call OpenAI-compatible image generation API."""
    url = _build_image_generations_url(settings.image_base_url)
    headers = _build_image_headers()
    timeout = float(settings.llm_timeout_seconds)

    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size or settings.image_size,
        "response_format": response_format,
    }

    logger.info("[IMAGE][once] request model=%s url=%s size=%s", model, url, payload["size"])

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

async def stream_chat(
    messages: list[dict],
    model: str,
    model_ack_callback: Callable[[dict], None] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream chat completion from Ollama/OpenAI-compatible API."""
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=True)
    headers = _build_headers()
    timeout = float(settings.llm_timeout_seconds)

    logger.info("[LLM][stream] request model=%s url=%s messages=%d", model, url, len(messages))

    full_content: list[str] = []
    ack_sent = False

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

                if not ack_sent:
                    actual_model = data.get("model")
                    _ack_response_model(expected_model=model, actual_model=actual_model, phase="stream")
                    if model_ack_callback is not None:
                        model_ack_callback({
                            "expected_model": model,
                            "actual_model": actual_model or "",
                            "matched": _models_match(model, actual_model) if actual_model else None,
                        })
                    ack_sent = True

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

    if not ack_sent:
        _ack_response_model(expected_model=model, actual_model=None, phase="stream")

    final_text = "".join(full_content)

    logger.info(
        "[LLM][stream] response done length=%d content=\n%s",
        len(final_text),
        final_text,
    )


_llm_health_cache: dict[str, object] = {"result": None, "checked_at": 0.0}
_llm_health_lock = asyncio.Lock()
_llm_health_refresh_task: asyncio.Task | None = None


def _build_models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/models"):
        return base
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


async def _probe_llm_models() -> dict:
    """Provider-agnostic quick check: verify the configured OpenAI-compatible models endpoint."""
    try:
        async with httpx.AsyncClient(timeout=float(settings.llm_health_timeout_seconds)) as client:
            response = await client.get(
                _build_models_url(settings.llm_base_url),
                headers=_auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            models = [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            return {"connected": True, "models": models, "stale": False}
    except Exception as exc:
        return {"connected": False, "error": str(exc), "models": [], "stale": False}


async def _refresh_llm_health_cache() -> dict:
    async with _llm_health_lock:
        result = await _probe_llm_models()
        _llm_health_cache["result"] = result
        _llm_health_cache["checked_at"] = time.monotonic()
        return result


def _schedule_llm_health_refresh() -> None:
    global _llm_health_refresh_task
    if _llm_health_refresh_task and not _llm_health_refresh_task.done():
        return
    _llm_health_refresh_task = asyncio.create_task(_refresh_llm_health_cache())


async def check_connection(*, deep: bool = False) -> dict:
    """Return cached LLM health quickly; refresh stale cache in background.

    The default quick check never performs model inference. A deep check is an
    explicit synchronous refresh hook for diagnostics or pre-generation gates;
    it still uses provider-agnostic configured endpoints rather than hard-coded
    provider/model assumptions.
    """
    ttl = max(0.0, float(settings.llm_health_cache_ttl_seconds))
    cached = _llm_health_cache.get("result")
    age = time.monotonic() - float(_llm_health_cache.get("checked_at") or 0.0)

    if deep:
        if _llm_health_lock.locked() and cached is not None:
            return {**cached, "stale": True, "refreshing": True}
        return await _refresh_llm_health_cache()

    if cached is None:
        _schedule_llm_health_refresh()
        return {"connected": None, "models": [], "stale": True, "refreshing": True, "status": "unknown"}

    if age <= ttl:
        return {**cached, "stale": False, "refreshing": False}

    _schedule_llm_health_refresh()
    return {**cached, "stale": True, "refreshing": True}


async def complete_json_object_once(
    *,
    messages: list[dict[str, str]],
    model: str,
    phase: str,
    response_schema: dict[str, Any],
) -> dict[str, Any]:
    """Call an OpenAI-compatible chat endpoint with strict JSON Schema output.

    The provider is asked to constrain generation to response_schema.

    There is no:
    - prose extraction;
    - Markdown stripping;
    - regex JSON recovery;
    - second LLM retry.

    The complete model content must itself be one JSON object.
    """

    base_url = str(
        settings.llm_base_url
        or ""
    ).rstrip("/")

    if not base_url:
        raise RuntimeError(
            "llm_base_url is empty"
        )

    if base_url.endswith(
        "/v1/chat/completions"
    ):
        url = base_url

    elif base_url.endswith("/v1"):
        url = (
            base_url
            + "/chat/completions"
        )

    else:
        url = (
            base_url
            + "/v1/chat/completions"
        )

    api_key = str(
        getattr(
            settings,
            "llm_api_key",
            "",
        )
        or getattr(
            settings,
            "openai_api_key",
            "",
        )
        or "ollama"
    ).strip()

    headers = {
        "Content-Type": (
            "application/json"
        ),
        "Authorization": (
            f"Bearer {api_key}"
        ),
    }

    schema_name = "".join(
        character
        if (
            character.isalnum()
            or character == "_"
        )
        else "_"
        for character in str(
            phase or ""
        )
    ).strip("_")

    schema_name = (
        schema_name[:64]
        or "creator_structured_output"
    )

    payload: dict[str, Any] = {
        "model": model,

        "messages": messages,

        "stream": False,

        "response_format": {
            "type": "json_schema",

            "json_schema": {
                "name": schema_name,

                "strict": True,

                "schema": (
                    response_schema
                ),
            },
        },

        "temperature": 0,
    }

    max_tokens = getattr(
        settings,
        "max_tokens",
        None,
    )

    if max_tokens is not None:
        payload[
            "max_tokens"
        ] = max_tokens

    timeout = float(
        getattr(
            settings,
            "llm_timeout_seconds",
            120,
        )
        or 120
    )

    logger.info(
        "[Creator]"
        "[json_once]"
        "[request] "
        "phase=%s "
        "model=%s "
        "url=%s "
        "messages=%d "
        "schema_name=%s "
        "schema=%s",
        phase,
        model,
        url,
        len(messages),
        schema_name,
        json.dumps(
            response_schema,
            ensure_ascii=False,
            default=str,
        ),
    )

    async with httpx.AsyncClient(
        timeout=timeout
    ) as client:
        response = await client.post(
            url,
            headers=headers,
            json=payload,
        )

    try:
        response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            "structured-output LLM request failed: "
            f"status={response.status_code} "
            f"body={response.text[:4000]}"
        ) from exc

    try:
        body = response.json()

    except Exception as exc:
        raise RuntimeError(
            "structured-output LLM response "
            "body is not JSON"
        ) from exc

    if not isinstance(
        body,
        dict,
    ):
        raise RuntimeError(
            "structured-output LLM response "
            "body is not an object"
        )

    choices = body.get(
        "choices"
    )

    if (
        not isinstance(
            choices,
            list,
        )
        or not choices
    ):
        raise RuntimeError(
            "structured-output LLM response "
            "does not contain choices"
        )

    choice = choices[0]

    if not isinstance(
        choice,
        dict,
    ):
        raise RuntimeError(
            "structured-output LLM first "
            "choice is not an object"
        )

    message = (
        choice.get("message")
        if isinstance(
            choice.get("message"),
            dict,
        )
        else {}
    )

    content = (
        message.get("content")
        or choice.get("text")
        or ""
    )

    if not isinstance(
        content,
        str,
    ):
        raise RuntimeError(
            "structured-output LLM content "
            "is not a string"
        )

    content = content.strip()

    if not content:
        raise RuntimeError(
            "structured-output LLM returned "
            "empty content"
        )

    logger.info(
        "[Creator]"
        "[json_once]"
        "[response] "
        "phase=%s "
        "expected_model=%s "
        "actual_model=%s "
        "finish_reason=%s "
        "content_len=%d "
        "content=\n%s",
        phase,
        model,
        str(
            body.get("model")
            or ""
        ),
        str(
            choice.get("finish_reason")
            or ""
        ),
        len(content),
        content[:8000],
    )

    try:
        parsed = json.loads(
            content
        )

    except json.JSONDecodeError as exc:
        raise ValueError(
            "structured-output LLM returned "
            "invalid JSON: "
            f"{exc}"
        ) from exc

    if not isinstance(
        parsed,
        dict,
    ):
        raise ValueError(
            "structured-output LLM response "
            "must be a JSON object"
        )

    return parsed
