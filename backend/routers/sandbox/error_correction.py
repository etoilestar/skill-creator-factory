"""LLM 错误纠正与重试。"""

import json
import logging
from pathlib import Path

from ...services.artifact_validator import (
    FileOutputValidationError,
    declared_artifact_paths,
    validate_stdout_file_outputs,
)
from ...services.llm_proxy import complete_chat_once
from ..chat_utils import (
    _NODE_BUILTIN_MODULES,
    _is_within_sandbox,
    _planner_model_name,
    _strip_markdown_json_fence,
)

logger = logging.getLogger(__name__)


_MAX_SANDBOX_RETRY = 3  # Maximum LLM-based retry attempts for failed sandbox tasks


def _compose_error_correction_prompt(
    *,
    task: dict,
    error_result: dict,
    attempt: int,
    max_retries: int,
) -> str:
    """Build a system prompt for LLM-based error correction."""
    action = task.get("action", "")
    return (
        "你是沙盒执行错误修正助手。\n\n"
        "一个 Skill 任务在沙盒环境中执行失败。你需要根据错误信息分析失败原因，"
        "并提供修正后的任务描述。\n\n"
        "重要规则：\n"
        "1. 只修正导致失败的参数（如命令、路径、参数值），不要改变任务的 action 类型。\n"
        "2. 修正后的命令或路径必须仍然在沙盒安全范围内。\n"
        "3. 如果错误是由于缺少文件或路径不存在，尝试修正路径。\n"
        "4. 如果错误是由于命令参数错误，尝试修正参数。\n"
        "5. 如果无法确定修正方案，将 corrected 设为 false。\n"
        "6. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        f"当前是第 {attempt}/{max_retries} 次重试。\n\n"
        "输出格式：\n"
        "{\n"
        '  "corrected": true,\n'
        '  "reason": "修正原因",\n'
        '  "task": { ... 修正后的完整 task 对象 ... }\n'
        "}\n\n"
        "如果无法修正：\n"
        "{\n"
        '  "corrected": false,\n'
        '  "reason": "无法修正的原因"\n'
        "}\n"
    )


def _parse_error_correction_decision(text: str) -> dict:
    """Parse the LLM error correction decision."""
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("error correction decision is not valid JSON: %s", text[:500])
        return {"corrected": False, "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"corrected": False, "reason": "输出不是 JSON object"}

    corrected = data.get("corrected", False)
    if isinstance(corrected, str):
        corrected = corrected.strip().lower() in {"true", "1", "yes", "y"}
    else:
        corrected = bool(corrected)

    if not corrected:
        return {
            "corrected": False,
            "reason": str(data.get("reason") or "").strip(),
        }

    corrected_task = data.get("task")
    if not isinstance(corrected_task, dict):
        return {"corrected": False, "reason": "corrected=true 但缺少有效的 task 对象"}

    return {
        "corrected": True,
        "reason": str(data.get("reason") or "").strip(),
        "task": corrected_task,
    }


async def _get_llm_error_correction(
    *,
    task: dict,
    error_result: dict,
    attempt: int,
    max_retries: int,
    body_prompt: str,
    model: str,
) -> dict:
    """Call LLM to analyze a sandbox execution error and suggest a correction."""
    system_prompt = _compose_error_correction_prompt(
        task=task,
        error_result=error_result,
        attempt=attempt,
        max_retries=max_retries,
    )

    error_context = {
        "failed_task": {
            "action": task.get("action"),
            "command": str(task.get("command") or "")[:500],
            "path": task.get("path"),
            "reason": task.get("reason"),
        },
        "error_result": {
            "success": error_result.get("success"),
            "returncode": error_result.get("returncode"),
            "stderr": str(error_result.get("stderr") or "")[:1000],
            "stdout": str(error_result.get("stdout") or "")[:500],
            "message": str(error_result.get("message") or "")[:500],
        },
        "attempt": attempt,
        "max_retries": max_retries,
    }

    skill_context = body_prompt[:2000] if body_prompt else ""

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "skill_context": skill_context,
                    "error_context": error_context,
                },
                ensure_ascii=False,
            ),
        },
    ]

    try:
        correction_text = await complete_chat_once(messages, _planner_model_name(model))
        return _parse_error_correction_decision(correction_text)
    except Exception as exc:
        logger.warning("LLM error correction call failed: %s", exc)
        return {"corrected": False, "reason": f"LLM 调用失败: {exc}"}


def _apply_error_correction(original_task: dict, correction: dict) -> dict:
    """Apply LLM-suggested correction to a failed task."""
    corrected_task = correction.get("task", {})
    if not isinstance(corrected_task, dict):
        return original_task

    merged = {**original_task, **corrected_task}
    merged["action"] = original_task.get("action", "")

    return merged


def _stdout_json_payload(stdout: str) -> dict | None:
    try:
        payload = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _validate_html_asset_outputs_in_generated_dir(stdout: str, *, cwd: Path | None) -> None:
    payload = _stdout_json_payload(stdout)
    if payload is None or cwd is None:
        raise ValueError("html_asset_builder stdout 必须是 JSON object，并包含 html_path 或 asset_paths")
    declared = []
    for key in ("html_path", "asset_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            declared.append(value.strip())
    for key in ("asset_paths", "html_paths"):
        value = payload.get(key)
        if isinstance(value, list):
            declared.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    if not declared:
        raise ValueError("html_asset_builder stdout JSON 必须包含 html_path 或 asset_paths")
    root = cwd.resolve()
    generated_root = (root / "assets" / "generated").resolve()
    for raw in declared:
        candidate = Path(raw)
        normalized_raw = raw.replace("\\", "/")
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve() if normalized_raw.startswith("assets/") else (root / "scripts" / candidate).resolve()
        try:
            candidate.relative_to(generated_root)
        except ValueError as exc:
            raise ValueError("html_asset_builder 输出路径必须位于当前 Skill 的 assets/generated/ 下") from exc
        if not candidate.is_file():
            raise ValueError(f"html_asset_builder 声明的输出文件不存在: {raw}")

def _output_files_from_stdout_json(stdout: str, *, cwd: Path | None, skill_name: str) -> list[dict]:
    """Extract generated artifact paths declared by script stdout JSON."""
    if cwd is None or not skill_name or not (stdout or "").strip():
        return []
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    raw_paths = [raw_path for _, raw_path in declared_artifact_paths(payload)]
    try:
        validated = validate_stdout_file_outputs(stdout, skill_dir=cwd, cwd=cwd / "scripts")
    except FileOutputValidationError:
        validated = []
    output_files: list[dict] = []
    seen: set[str] = set()
    for item in validated:
        rel = item["path"]
        if rel in seen:
            continue
        seen.add(rel)
        output_files.append({"path": rel, "url": f"/api/skills/{skill_name}/files/{rel}"})

    root = cwd.resolve()
    for raw in raw_paths:
        candidate = Path(raw)
        normalized_raw = raw.replace("\\", "/")
        if Path(raw).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            continue
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve() if normalized_raw.startswith(("assets/", "outputs/")) else (root / "scripts" / candidate).resolve()
        if not _is_within_sandbox(candidate, root) or not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        output_files.append({"path": rel, "url": f"/api/skills/{skill_name}/files/{rel}"})
    return output_files


# Public aliases
parse_error_correction_decision = _parse_error_correction_decision
apply_error_correction = _apply_error_correction
compose_error_correction_prompt = _compose_error_correction_prompt
output_files_from_stdout_json = _output_files_from_stdout_json
