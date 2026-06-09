"""Stdout 验证与渲染。"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _validate_structured_stdout_payload(payload: dict) -> None:
    """Validate JSON stdout fields consumed by sandbox UI/finalization."""
    if "text" in payload and not isinstance(payload.get("text"), str):
        raise ValueError("stdout JSON 字段 text 必须是字符串")

    if "image_paths" in payload:
        image_paths = payload.get("image_paths")
        if not isinstance(image_paths, list):
            raise ValueError("stdout JSON 字段 image_paths 必须是 list[str]")
        for path in image_paths:
            if not isinstance(path, str):
                raise ValueError("stdout JSON 字段 image_paths 的每一项都必须是字符串")

    if "images" in payload:
        images = payload.get("images")
        if not isinstance(images, list):
            raise ValueError("stdout JSON 字段 images 必须是 list[dict]")
        for image in images:
            if not isinstance(image, dict):
                raise ValueError("stdout JSON 字段 images 的每一项都必须是 object")
            if "image_path" in image and not isinstance(image.get("image_path"), str):
                raise ValueError("stdout JSON 字段 images[].image_path 必须是字符串")


def _validate_success_stdout_json_if_structured(stdout: str) -> None:
    """Validate structured JSON stdout without rejecting legacy plain text."""
    stripped = (stdout or "").strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    if "error" in payload:
        raise ValueError("stdout JSON 不得包含 error 字段")
    if any(key in payload for key in ("text", "image_paths", "images")):
        _validate_structured_stdout_payload(payload)


def _payload_image_paths(payload: dict) -> list[str]:
    paths: list[str] = []

    for key in ("image_path", "image"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())

    image_paths = payload.get("image_paths")
    if isinstance(image_paths, list):
        paths.extend(path.strip() for path in image_paths if isinstance(path, str) and path.strip())

    images = payload.get("images")
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                path = image.get("image_path")
                if isinstance(path, str) and path.strip():
                    paths.append(path.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _render_success_stdout_payload(result: dict) -> str | None:
    """Render structured JSON stdout from a successful command as final user content."""
    for item in result.get("results") or []:
        if not isinstance(item, dict) or not item.get("success"):
            continue
        stdout = str(item.get("stdout") or "").strip()
        if not stdout:
            continue
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            _validate_structured_stdout_payload(payload)
        except ValueError:
            continue
        text = str(payload.get("text") or payload.get("markdown") or "").strip()
        image_paths = _payload_image_paths(payload)
        parts: list[str] = []
        if text:
            parts.append(text)
        for image_path in image_paths:
            if image_path not in text:
                parts.append(f"![插图]({image_path})")
        if parts:
            return "\n\n".join(parts)
    return None


def _format_execution_report(result: dict) -> str:
    if not result.get("executed"):
        reason = result.get("reason", "未知原因")
        errors = result.get("plan", {}).get("errors", []) if isinstance(result.get("plan"), dict) else []
        if errors:
            rendered_errors = "\n".join(f"- {json.dumps(item, ensure_ascii=False)}" for item in errors)
            return f"\n\n⚠️ 后台未执行规划任务：{reason}\n规划提示：\n{rendered_errors}"
        return f"\n\n⚠️ 后台未执行规划任务：{reason}"

    logs = result.get("logs") or []

    if not logs:
        for item in result.get("results", []):
            action = item.get("action")
            if action == "read_resource":
                logs.append(f"读取资源: {item.get('path')}")
            elif action == "write_file":
                logs.append(f"写入文件: {item.get('path')}")
            elif action == "run_command":
                logs.append(f"执行命令成功: {item.get('command')}")
            elif action == "create_directory":
                logs.append(f"创建目录: {item.get('path')}")

    if not logs:
        return "\n\n✅ 后台已执行规划任务。"

    rendered = "\n".join(f"- {line}" for line in logs)
    return f"\n\n✅ 后台已执行规划任务：\n{rendered}"


# Public aliases
render_success_stdout_payload = _render_success_stdout_payload
validate_success_stdout_json_if_structured = _validate_success_stdout_json_if_structured
