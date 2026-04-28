import httpx
import json
from typing import AsyncGenerator

from ..config import settings


def _auth_headers() -> dict:
    """Return Authorization header when an OpenAI API key is configured."""
    if settings.openai_api_key:
        return {"Authorization": f"Bearer {settings.openai_api_key}"}
    return {}


async def stream_chat(messages: list[dict], model: str) -> AsyncGenerator[str, None]:
    """Stream chat completion from Ollama/LM Studio or OpenAI via OpenAI-compatible API."""
    url = f"{settings.llm_base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=_auth_headers()) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                except json.JSONDecodeError:
                    continue


async def check_connection() -> dict:
    """Check if the LLM backend is reachable and return available models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.llm_base_url}/v1/models", headers=_auth_headers()
            )
            response.raise_for_status()
            data = response.json()
            models = [m["id"] for m in data.get("data", [])]
            return {"connected": True, "models": models}
    except Exception as exc:
        return {"connected": False, "error": str(exc), "models": []}
