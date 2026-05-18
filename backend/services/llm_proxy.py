import httpx
import json
import os
import logging
from collections.abc import AsyncGenerator

from ..config import settings


logger = logging.getLogger(__name__)


def _resolve_api_key() -> str | None:
    """Resolve the LLM API key from config or environment, returning None if absent."""
    return (
        settings.llm_api_key
        or settings.openai_api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
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


def _get_api_key() -> str:
    """Ollama ignores the key, but OpenAI-compatible services usually expect one."""
    return _resolve_api_key() or "ollama"


def _build_payload(
    *,
    messages: list[dict],
    model: str,
    stream: bool,
    temperature: float | None = None,
    response_format: dict | None = None,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    # Per-call temperature takes precedence over the global setting.
    effective_temp = temperature if temperature is not None else settings.temperature
    if effective_temp is not None:
        payload["temperature"] = effective_temp

    if settings.max_tokens is not None:
        payload["max_tokens"] = settings.max_tokens

    if response_format is not None:
        payload["response_format"] = response_format

    return payload


def _build_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_api_key()}",
    }


async def complete_chat_once(
    messages: list[dict],
    model: str,
    *,
    temperature: float | None = None,
    response_format: dict | None = None,
) -> str:
    """Non-streaming chat completion.

    用于 metadata 阶段的静默模型调用。

    Args:
        temperature: Overrides the global ``settings.temperature`` for this call.
            Pass ``0.0`` for deterministic JSON-only rounds.
        response_format: Optional structured-output hint, e.g.
            ``{"type": "json_object"}``.  Omit for natural-language responses.
            Backends that do not support this parameter will raise an
            ``httpx.HTTPStatusError`` with status 400 or 422; callers that need
            graceful fallback should use :func:`complete_chat_once_with_json_retry`.
    """
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(
        messages=messages,
        model=model,
        stream=False,
        temperature=temperature,
        response_format=response_format,
    )
    headers = _build_headers()
    timeout = float(settings.llm_timeout_seconds)

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


# ---------------------------------------------------------------------------
# JSON-retry helpers
# ---------------------------------------------------------------------------

_JSON_CORRECTION_PROMPT = (
    "你的上一次回复包含了自然语言或 Markdown，不是合法的 JSON。\n"
    "请重新输出，只输出一个符合格式要求的 JSON 对象，"
    "不要任何解释、不要 Markdown、不要代码块标记。\n"
    "直接输出 { ... }，不要其他内容。"
)

# HTTP status codes returned by backends that do not support `response_format`.
_RESPONSE_FORMAT_UNSUPPORTED_CODES = (400, 422)


def _looks_like_valid_json(text: str) -> bool:
    """Return True if *text* (after stripping common markdown fences) is valid JSON.

    Handled fence patterns (checked in order):
    - ``\\`\\`\\`json ... \\`\\`\\``` — the most common structured-output wrapper.
    - ``\\`\\`\\` ... \\`\\`\\``` — bare code fence whose content is JSON.
    - Raw JSON (no fence) — verified with ``json.loads``.

    This is a lightweight check used by :func:`complete_chat_once_with_json_retry`
    to decide whether a retry is needed.  The full ``_strip_markdown_json_fence``
    in ``chat.py`` handles additional edge-cases (embedded fences, bracket-depth
    scan) for the final parse.
    """
    stripped = text.strip()
    for prefix in ("```json", "```"):
        if stripped.startswith(prefix):
            stripped = stripped.removeprefix(prefix).strip()
            if stripped.endswith("```"):
                stripped = stripped[:-3].strip()
            break
    try:
        json.loads(stripped)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


async def complete_chat_once_with_json_retry(
    messages: list[dict],
    model: str,
    *,
    temperature: float | None = None,
    response_format: dict | None = None,
    max_retries: int = 1,
) -> str:
    """Non-streaming chat completion with automatic JSON correction retry.

    Strategy
    --------
    1. Call the model with the optional *response_format* and *temperature*
       override.
    2. If the backend rejects *response_format* with HTTP 400 or 422 (i.e. the
       model / backend does not support structured output), transparently retry
       the same request **without** ``response_format``.
    3. If the response does not look like valid JSON, append a correction prompt
       and retry up to *max_retries* times (using ``temperature=0.0`` for the
       correction round to reduce verbosity).

    Returns the raw model text string.  Callers are still responsible for the
    final ``json.loads`` call and for using ``_strip_markdown_json_fence`` as a
    last-resort fallback before raising.
    """
    effective_response_format = response_format

    # First attempt — try with response_format; fall back gracefully if the
    # backend signals it is unsupported (HTTP 400 / 422).
    try:
        text = await complete_chat_once(
            messages,
            model,
            temperature=temperature,
            response_format=effective_response_format,
        )
    except httpx.HTTPStatusError as exc:
        if effective_response_format is not None and exc.response.status_code in _RESPONSE_FORMAT_UNSUPPORTED_CODES:
            logger.warning(
                "[LLM][json-retry] response_format rejected by backend (HTTP %d),"
                " retrying without it",
                exc.response.status_code,
            )
            effective_response_format = None
            text = await complete_chat_once(messages, model, temperature=temperature)
        else:
            raise

    if _looks_like_valid_json(text):
        return text

    # Correction retry loop.
    current_messages = list(messages)
    for attempt in range(max_retries):
        logger.warning(
            "[LLM][json-retry] attempt %d/%d — non-JSON response: %s",
            attempt + 1,
            max_retries,
            text[:300],
        )
        current_messages.extend([
            {"role": "assistant", "content": text},
            {"role": "user", "content": _JSON_CORRECTION_PROMPT},
        ])
        text = await complete_chat_once(
            current_messages,
            model,
            temperature=0.0,
            response_format=effective_response_format,
        )
        if _looks_like_valid_json(text):
            return text

    logger.warning(
        "[LLM][json-retry] all %d retries exhausted, returning last response as-is",
        max_retries,
    )
    return text


async def stream_chat(messages: list[dict], model: str) -> AsyncGenerator[str, None]:
    """Stream chat completion from Ollama/OpenAI-compatible API."""
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=True)
    headers = _build_headers()
    timeout = float(settings.llm_timeout_seconds)

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