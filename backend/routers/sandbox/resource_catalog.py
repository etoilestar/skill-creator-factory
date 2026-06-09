"""资源目录提取与 LLM 资源选择。"""

import json
import logging
import re
from pathlib import Path

from ...services.llm_proxy import complete_chat_once
from ..chat_utils import (
    _is_within_sandbox,
    _allowed_skill_roots,
    _planner_model_name,
    _request_messages_with_files,
    _last_user_text,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest
from .path_resolution import _normalize_skill_resource_path

logger = logging.getLogger(__name__)


def _extract_runtime_resource_catalog(body_prompt: str, *, execution_root: "Path | None" = None) -> list[dict]:
    """Extract host-owned resource catalog from Loaded SKILL.md prompt.

    关键原则：
    - 真实 path 只归宿主管理；
    - planner 只能看到 resource_handle；
    - planner 不能自己生成 read_resource.path。

    策略：
    1. 用宽松正则匹配所有 backtick 引用（列表、表格、行内等写法均可识别）。
    2. 若传入 execution_root，从磁盘直接扫 scripts/、references/、assets/ 三个子目录，
       将未被正则发现的文件追加进 catalog（彻底兜底）。
    """
    catalog: list[dict] = []
    seen: set[str] = set()

    # 宽松正则：匹配所有被 backtick 包裹的 references/assets/scripts 路径
    # 覆盖列表（- `scripts/xxx`）、表格单元格、行内引用等写法
    # 可选地捕获紧随其后的「：标题」（兼容旧的列表格式）
    pattern = re.compile(
        r"`(?P<path>(references|assets|scripts)/[^`]+)`(?P<title>：[^\n]+)?",
        re.M,
    )

    def _add_entry(path: str, title: str = "") -> None:
        path = _normalize_skill_resource_path(path)
        if path in seen:
            return
        kind = path.split("/", 1)[0]
        if kind not in {"references", "assets", "scripts"}:
            return
        if execution_root is not None:
            root = execution_root.resolve()
            candidate = (root / path).resolve()
            if not _is_within_sandbox(candidate, root) or not candidate.is_file():
                logger.info("skip missing/non-local skill resource from catalog: root=%s path=%s", root, path)
                return
        seen.add(path)
        if kind == "references":
            allowed_actions = ["read_resource"]
            usage_hint = "参考资料，可在任务需要领域知识、示例、规范时读取。"
        elif kind == "assets":
            allowed_actions = ["read_resource"]
            usage_hint = "模板或配置，可在任务需要固定格式、配置、模板时读取。"
        else:
            allowed_actions = ["run_command"]
            usage_hint = "脚本资源，默认用于执行，不用于读取源码，除非用户明确要求查看脚本内容。"
        catalog.append(
            {
                "resource_handle": f"resource:{len(catalog)}",
                "kind": kind,
                "path": path,
                "title": title,
                "allowed_actions": allowed_actions,
                "usage_hint": usage_hint,
            }
        )

    for match in pattern.finditer(body_prompt):
        title = (match.group("title") or "").lstrip("：").strip()
        _add_entry(match.group("path").strip(), title)

    # 文件系统兜底：扫描磁盘上真实存在的文件，补充正则未捕获的条目
    if execution_root is not None:
        execution_root_resolved = execution_root.resolve()
        # Guard: only scan if execution_root itself is within an allowed root.
        if any(_is_within_sandbox(execution_root_resolved, r.resolve()) for r in _allowed_skill_roots()):
            for subdir in ("scripts", "references", "assets"):
                scan_dir = execution_root_resolved / subdir
                if not scan_dir.is_dir():
                    continue
                for entry in sorted(scan_dir.iterdir()):
                    # Reject symlinks that escape the skill sandbox
                    if not _is_within_sandbox(entry, execution_root_resolved):
                        continue
                    if entry.is_file():
                        _add_entry(f"{subdir}/{entry.name}")

    return catalog

def _resource_catalog_for_planner(catalog: list[dict]) -> list[dict]:
    """Expose resource tree to planner without exposing executable paths for read_resource."""
    return [
        {
            "resource_handle": item["resource_handle"],
            "display_path": item.get("path", ""),
            "kind": item["kind"],
            "title": item.get("title", ""),
            "allowed_actions": item.get("allowed_actions", []),
            "usage_hint": item.get("usage_hint", ""),
        }
        for item in catalog
    ]

def _resource_catalog_by_handle(catalog: list[dict]) -> dict[str, dict]:
    return {str(item["resource_handle"]): item for item in catalog}


def _resolve_resource_handle_alias(value: str, resource_catalog: list[dict]) -> tuple[str | None, dict | None, bool]:
    """Resolve real resource handles and path-like pseudo handles deterministically.

    Returns (canonical_handle, catalog_item, was_alias).  The resolver accepts
    model mistakes such as ``resource:story_templates.md`` only when they map
    uniquely to a catalog path/basename.
    """
    raw = str(value or "").strip()
    if not raw:
        return None, None, False
    by_handle = _resource_catalog_by_handle(resource_catalog)
    if raw in by_handle:
        return raw, by_handle[raw], False

    alias = raw.replace("\\", "/").strip().strip("`'\"").lstrip("./")
    if alias.startswith("resource:"):
        suffix = alias.split(":", 1)[1].strip().lstrip("./")
        # resource:0 is a real handle shape; if it was not found above, do not
        # reinterpret it as a path-like alias.
        if suffix.isdigit():
            return None, None, False
        alias = suffix

    if not alias:
        return None, None, False
    candidates: list[dict] = []
    for item in resource_catalog:
        path = _normalize_skill_resource_path(str(item.get("path") or ""))
        basename = Path(path).name
        if alias == path or alias == basename or alias.endswith("/" + basename):
            candidates.append(item)
    if len(candidates) != 1:
        return None, None, False
    item = candidates[0]
    handle = str(item.get("resource_handle") or "")
    return (handle, item, True) if handle else (None, None, False)

def _compose_resource_selection_prompt() -> str:
    return (
        "你是 Skill 资源按需加载选择器。\n\n"
        "你会看到 Loaded SKILL.md、resource_catalog 和用户请求。"
        "你的任务是判断当前阶段是否需要读取 references/assets/scripts 中的资源正文。\n\n"
        "重要规则：\n"
        "1. 只能从 resource_catalog 中选择真实存在的 resource_handle，例如 resource:0。\n"
        "2. 禁止生成、拼接、改写资源 path；禁止输出 resource:<filename> 或 resource:<path> 这种伪 handle。\n"
        "3. display_path 只帮助你理解 resource:0 对应哪个文件，action 中仍只能输出 resource_handle。\n"
        "4. references 通常用于方法论、规范、示例；scripts 默认用于执行，不要读取源码，除非用户明确要求查看脚本内容。\n"
        "5. assets 在需要模板或配置时读取。\n"
        "6. 如果 SKILL.md body 已经足够完成任务，可以不读取资源。\n"
        "7. 最多选择 5 个资源，避免一次加载过多。\n"
        "8. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"need_resources\": true,\n"
        "  \"resource_handles\": [\"resource:0\", \"resource:1\"],\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n\n"
        "如果不需要资源：\n"
        "{\n"
        "  \"need_resources\": false,\n"
        "  \"resource_handles\": [],\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n"
    )

def _parse_resource_selection_decision(
    text: str,
    *,
    resource_catalog: list[dict],
) -> dict:
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("resource selection decision is not valid JSON: %s", text[:500])
        return {"need_resources": False, "resource_handles": [], "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"need_resources": False, "resource_handles": [], "reason": "输出不是 JSON object"}

    need_resources = data.get("need_resources", False)
    if isinstance(need_resources, str):
        need_resources = need_resources.strip().lower() in {"true", "1", "yes", "y"}
    else:
        need_resources = bool(need_resources)

    raw_handles = data.get("resource_handles", [])

    if not isinstance(raw_handles, list):
        raw_handles = []

    selected: list[str] = []
    for item in raw_handles:
        handle = str(item or "").strip()
        if not handle:
            continue
        canonical, _resource, _was_alias = _resolve_resource_handle_alias(handle, resource_catalog)
        if not canonical:
            continue
        if canonical not in selected:
            selected.append(canonical)
        if len(selected) >= 5:
            break

    if not need_resources or not selected:
        return {
            "need_resources": False,
            "resource_handles": [],
            "reason": str(data.get("reason") or "").strip(),
        }

    return {
        "need_resources": True,
        "resource_handles": selected,
        "reason": str(data.get("reason") or "").strip(),
    }

async def _run_resource_selection_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    resource_catalog: list[dict],
) -> dict:
    if not resource_catalog:
        return {"need_resources": False, "resource_handles": [], "reason": "无可用资源"}

    messages = [
        {"role": "system", "content": _compose_resource_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "resource_catalog": _resource_catalog_for_planner(resource_catalog),
                    "user_messages": _request_messages_with_files(request),
                    "last_user_text": _last_user_text(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    decision_text = await complete_chat_once(messages, _planner_model_name(model))
    return _parse_resource_selection_decision(
        decision_text,
        resource_catalog=resource_catalog,
    )


# Public aliases
extract_runtime_resource_catalog = _extract_runtime_resource_catalog
parse_resource_selection_decision = _parse_resource_selection_decision
resource_catalog_for_planner = _resource_catalog_for_planner
