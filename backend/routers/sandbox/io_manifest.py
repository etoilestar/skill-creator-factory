"""Sandbox-only adapter for Platform IO inputs and uploaded files."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from pydantic import Field

from ..chat_models import ChatRequest
from ..chat_utils import _is_within_sandbox, _last_user_text

MAX_PREVIEW_BYTES = 100 * 1024


class SandboxChatRequest(ChatRequest):
    """Add existing Platform input slots without changing shared ChatRequest."""

    payload: Any | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    resources: list[Any] = Field(default_factory=list)


def _safe_text_preview(path: Path, limit: int = MAX_PREVIEW_BYTES) -> str | None:
    """Return a bounded UTF-8 preview when bytes are demonstrably text."""
    if not path.is_file() or path.stat().st_size > limit:
        return None
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    control = sum(ord(char) < 32 and char not in "\n\r\t" for char in text)
    if text and control / len(text) > 0.01:
        return None
    return text


def build_input_envelope(request: SandboxChatRequest, execution_root: Path | None) -> dict:
    text = _last_user_text(request) or ""
    root = execution_root.resolve() if execution_root else None
    manifests: list[dict] = []
    for item in request.input_files or []:
        rel = str(item.get("path") or "")
        filename = str(item.get("filename") or Path(rel).name)
        mime = str(item.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        path = (root / rel).resolve() if root and rel else None
        safe = bool(path and _is_within_sandbox(path, root) and path.is_file())
        preview = _safe_text_preview(path) if safe else None
        manifests.append({
            "path": rel,
            "filename": filename,
            "size": path.stat().st_size if safe else int(item.get("size") or 0),
            "mime_type": mime,
            "media_family": mime.partition("/")[0] + "/*",
            "model_access": "inline_image" if mime.startswith("image/") and safe else ("text_preview" if preview is not None else "path_only"),
            "relative_to_input_dir": "/".join(Path(rel).parts[1:]) if len(Path(rel).parts) > 1 else filename,
            "relative_to_input_session_dir": "/".join(Path(rel).parts[2:]) if len(Path(rel).parts) > 2 else filename,
            **({"text_preview": preview} if preview is not None else {}),
        })
    return {
        "user_request": text, "input": text, "text": text,
        "payload": getattr(request, "payload", None),
        "fields": getattr(request, "fields", {}) or {},
        "options": getattr(request, "options", {}) or {},
        "input_files": manifests, "files": manifests,
        "resources": getattr(request, "resources", []) or [],
    }
