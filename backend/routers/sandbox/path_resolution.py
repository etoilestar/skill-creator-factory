"""路径解析与沙箱边界检查。

主要消费者：sandbox_chat.py 的 _make_stream 函数
公共 API：available_scripts_for_root（被测试文件引用）
内部函数：其余函数仅被 sandbox 子包内部使用
"""

import shlex
from pathlib import Path
from typing import Any

from ...services.skill_manager import get_execution_skill_dir
from ..chat_utils import _allowed_skill_roots, _is_within_sandbox

logger = __import__("logging").getLogger(__name__)


def _skill_root_for_name(skill_name: str) -> Path:
    """Resolve an existing sandbox skill root by skill_name."""
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise ValueError(f"非法 skill_name: {skill_name}")
    return get_execution_skill_dir(skill_name, mode="sandbox").resolve()

def _workflow_value_preview(value: Any, *, max_len: int = 300) -> dict[str, Any]:
    try:
        if isinstance(value, list):
            first = value[0] if value else None
            preview: dict[str, Any] = {
                "type": "list",
                "len": len(value),
                "first_type": type(first).__name__ if value else None,
            }
            if isinstance(first, dict):
                preview["first_keys"] = sorted(str(k) for k in first.keys())[:20]
                preview["first_preview"] = str(first)[:max_len]
            elif first is not None:
                preview["first_preview"] = str(first)[:max_len]
            return preview

        if isinstance(value, dict):
            return {
                "type": "dict",
                "keys": sorted(str(k) for k in value.keys())[:30],
                "preview": str(value)[:max_len],
            }

        if isinstance(value, str):
            return {
                "type": "str",
                "len": len(value),
                "preview": value[:max_len],
            }

        return {
            "type": type(value).__name__,
            "preview": str(value)[:max_len],
        }
    except Exception as exc:
        return {
            "type": type(value).__name__,
            "preview_error": str(exc),
        }


def _workflow_payload_summary(payload: dict[str, Any] | None, *, max_len: int = 300) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): _workflow_value_preview(value, max_len=max_len)
        for key, value in payload.items()
    }

def _available_scripts_for_root(execution_root: Path | None) -> list[str]:
    """Return real scripts under the current business Skill root only."""
    if execution_root is None:
        return []
    root = execution_root.resolve()
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir() or not _is_within_sandbox(scripts_dir, root):
        return []
    scripts: list[str] = []
    for entry in sorted(scripts_dir.iterdir()):
        if not entry.is_file():
            continue
        resolved = entry.resolve()
        if _is_within_sandbox(resolved, root):
            scripts.append(f"scripts/{entry.name}")
    return scripts

def _resolve_safe_path(raw_path: str, base_dir: Path | None = None) -> Path:
    """Resolve file paths and ensure they stay within allowed directories.

    确保文件路径是相对于 skill 根目录的，而不是宿主目录。
    """
    path = Path(raw_path).expanduser()

    if path.is_absolute():
        return path

    # 如果是相对路径，基于 execution_root 或者 inferred_skill_root 解析路径
    base_dir = base_dir or Path.cwd()
    return base_dir / path


def _looks_like_skill_resource_dir(path: Path) -> bool:
    return path.name in {"scripts", "references", "assets"}


def _normalize_skill_resource_path(value: str) -> str:
    """Normalize a skill-local resource path for catalog/loaded-path comparison."""
    normalized = str(value or "").strip().replace("\\", "/").lstrip("./")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized

def _infer_skill_root_from_tasks(plan: dict, *, execution_root: Path | None = None) -> Path | None:
    """Infer the active skill root from create_directory/write_file tasks.

    用于 /creator legacy fallback：
    如果模型先创建了 <skill-root>/scripts、references、assets，
    后续相对写入 SKILL.md、scripts/main.py 都应以 <skill-root> 为根。
    """
    candidates: list[Path] = []

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            continue

        try:
            resolved = _resolve_safe_path(raw_path, base_dir=execution_root)
        except Exception:
            continue

        if action == "create_directory":
            if _looks_like_skill_resource_dir(resolved):
                candidates.append(resolved.parent)
            else:
                candidates.append(resolved)

        elif action == "write_file":
            if resolved.name == "SKILL.md":
                candidates.append(resolved.parent)
            elif resolved.parent.name in {"scripts", "references", "assets"}:
                candidates.append(resolved.parent.parent)

    if not candidates:
        return None

    # 优先选择位于 allowed skill roots 下的最深目录
    allowed_roots = _allowed_skill_roots()
    valid: list[Path] = []

    for candidate in candidates:
        for allowed_root in allowed_roots:
            try:
                candidate.resolve().relative_to(allowed_root.resolve())
                valid.append(candidate.resolve())
                break
            except ValueError:
                continue

    if not valid:
        return None

    return sorted(valid, key=lambda p: len(p.parts), reverse=True)[0]

def _resolve_planned_file_path(
    raw_path: str,
    *,
    execution_root: Path | None = None,
    inferred_skill_root: Path | None = None,
) -> Path:
    """Resolve file path for planned write/create actions.

    规则：
    - 绝对路径保持绝对路径；
    - sandbox 有 execution_root 时，相对路径基于 execution_root；
    - creator 推断出 inferred_skill_root 时，Skill 内部相对路径基于 inferred_skill_root；
    - 否则退回原有逻辑。
    """
    path = Path(raw_path).expanduser()

    if path.is_absolute():
        return _resolve_safe_path(raw_path, base_dir=execution_root)

    if inferred_skill_root is not None:
        first = path.parts[0] if path.parts else ""

        # SKILL.md、scripts/main.py、references/xx、assets/xx 都属于当前 skill 根
        if raw_path == "SKILL.md" or first in {"scripts", "references", "assets"}:
            return _resolve_safe_path(raw_path, base_dir=inferred_skill_root)

    return _resolve_safe_path(raw_path, base_dir=execution_root)

def _parse_path_argument(path_expr: str) -> str:
    try:
        parts = shlex.split(path_expr)
    except ValueError as exc:
        raise ValueError(f"路径参数解析失败: {path_expr}") from exc

    if len(parts) != 1:
        raise ValueError(f"只允许一个路径参数: {path_expr}")

    return parts[0]


# Public alias for test imports
available_scripts_for_root = _available_scripts_for_root
