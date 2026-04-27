"""Ollama LLM client. Wraps HTTP calls with timeout and retry."""
import json
import time
from typing import Optional

import httpx

from .config import LLM_HOST, LLM_PORT, LLM_MODEL

_BASE_URL = f"http://{LLM_HOST}:{LLM_PORT}"
_TIMEOUT = 30.0
_MAX_RETRIES = 2


def chat(prompt: str, model: Optional[str] = None) -> str:
    """Send a prompt to Ollama and return the response text.
    
    Returns "[LLM_ERROR: ...]" on failure.
    """
    model = model or LLM_MODEL
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(f"{_BASE_URL}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data.get("message", {}).get("content", "")
        except httpx.TimeoutException as e:
            last_error = f"timeout: {e}"
        except httpx.HTTPStatusError as e:
            last_error = f"http {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            last_error = str(e)
        if attempt < _MAX_RETRIES:
            time.sleep(1)
    return f"[LLM_ERROR: {last_error}]"
