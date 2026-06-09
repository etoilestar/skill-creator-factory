"""资源读取与加载提示。"""

import logging
from pathlib import Path

from ...config import settings
from ...services.kernel_loader import read_skill_resource_text
from ..chat_utils import _is_within_sandbox
from .path_resolution import _normalize_skill_resource_path
from .resource_catalog import _resource_catalog_by_handle

logger = logging.getLogger(__name__)


def _read_runtime_skill_resource_text(
    *,
    skill_name: str,
    path: str,
    execution_root: Path | None = None,
) -> dict:
    """Read a skill-local resource from the execution root when available."""
    rel_path = _normalize_skill_resource_path(path)
    if execution_root is not None:
        root = execution_root.resolve()
        resource_path = (root / rel_path).resolve()
        if not _is_within_sandbox(resource_path, root) or not resource_path.is_file():
            raise FileNotFoundError(f"resource does not exist in current skill: {rel_path}")
        raw = resource_path.read_text(encoding="utf-8", errors="replace")
        max_chars = settings.skill_resource_max_chars
        return {
            "content": raw[:max_chars],
            "truncated": len(raw) > max_chars,
        }

    if not skill_name:
        raise ValueError("读取 Skill 资源需要 skill_name 或 execution_root")

    return read_skill_resource_text(
        skill_name,
        rel_path,
        max_chars=settings.skill_resource_max_chars,
    )


def _compose_loaded_resources_prompt(
    *,
    skill_name: str,
    resource_catalog: list[dict],
    selected_handles: list[str],
    execution_root: Path | None = None,
) -> dict:
    resource_by_handle = _resource_catalog_by_handle(resource_catalog)
    sections: list[str] = []
    loaded_paths: list[str] = []
    failed_paths: list[dict] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(handle)
        if not resource:
            failed_paths.append({
                "resource_handle": handle,
                "path": "",
                "missing_type": "planner_inconsistent",
                "reason": "selected resource_handle is not in resource_catalog",
            })
            continue

        path = _normalize_skill_resource_path(resource["path"])
        try:
            observation = _read_runtime_skill_resource_text(
                skill_name=skill_name,
                path=path,
                execution_root=execution_root,
            )
        except FileNotFoundError as exc:
            failed_paths.append({
                "resource_handle": handle,
                "path": path,
                "missing_type": "file_missing",
                "reason": str(exc),
            })
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error_type: file_missing\n"
                f"- load_error: {exc}\n"
            )
            continue
        except Exception as exc:
            failed_paths.append({
                "resource_handle": handle,
                "path": path,
                "missing_type": "load_failed",
                "reason": str(exc),
            })
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error_type: load_failed\n"
                f"- load_error: {exc}\n"
            )
            continue

        loaded_paths.append(path)
        content = observation.get("content", "")
        truncated = observation.get("truncated", False)

        sections.append(
            f"### {handle}\n"
            f"- kind: {resource.get('kind')}\n"
            f"- path: `{path}`\n"
            f"- truncated: {truncated}\n\n"
            "```text\n"
            f"{content}\n"
            "```"
        )

    prompt = ""
    if sections:
        prompt = (
            "\n\n---\n\n"
            "## Loaded On-Demand Resources\n\n"
            "以下资源由宿主根据当前请求按需读取。"
            "这些内容现在可以作为执行当前 Skill 的依据。"
            "如果资源已成功加载，后续规划不得再把它标记为 missing。\n\n"
            + "\n\n".join(sections)
        )

    return {
        "prompt": prompt,
        "loaded_paths": loaded_paths,
        "failed_paths": failed_paths,
    }


# Public alias
compose_loaded_resources_prompt = _compose_loaded_resources_prompt
