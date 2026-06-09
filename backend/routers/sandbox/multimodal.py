"""多模态图片嵌入与清单。"""

import base64
import mimetypes
from pathlib import Path

from ..chat_utils import _is_within_sandbox, _request_messages_with_files
from ..chat_models import ChatRequest

logger = __import__("logging").getLogger(__name__)


_VISION_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _request_messages_with_inline_images(request: ChatRequest, execution_root: Path | None) -> list[dict]:
    """Build OpenAI-compatible multimodal user messages for VL models."""
    messages = _request_messages_with_files(request)
    if execution_root is None or not request.input_files:
        return messages

    image_parts: list[dict] = []
    root = execution_root.resolve()
    for item in request.input_files:
        rel = str(item.get("path") or "")
        path = (root / rel).resolve()
        if path.suffix.lower() not in _VISION_IMAGE_EXTS:
            continue
        if not _is_within_sandbox(path, root) or not path.is_file():
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        })

    if not image_parts:
        return messages

    for i in reversed(range(len(messages))):
        if messages[i].get("role") == "user":
            text = str(messages[i].get("content") or "")
            messages[i] = {
                "role": "user",
                "content": [{"type": "text", "text": text}, *image_parts],
            }
            break
    return messages


def _strip_runtime_resource_manifest(body_prompt: str) -> str:
    """Remove generated resource manifest section from planner text.

    避免 planner 从 Markdown 资源清单中拼接路径。
    真实资源树通过 resource_catalog 单独传入。
    """
    marker = "## Bundled Resources Manifest"
    index = body_prompt.find(marker)
    if index < 0:
        return body_prompt

    before = body_prompt[:index].rstrip()
    return (
        before
        + "\n\n---\n\n"
        + "## Bundled Resources Manifest\n\n"
        + "资源清单已由宿主以结构化 resource_catalog 单独提供。"
        + "规划 read_resource 时只能使用 resource_handle，不能生成 path。\n"
    )


# Public alias
request_messages_with_inline_images = _request_messages_with_inline_images
