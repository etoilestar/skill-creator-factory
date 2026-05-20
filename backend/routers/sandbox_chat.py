"""Sandbox-mode chat helpers, planners, and execution routines."""

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..services.kernel_loader import (
    load_child_skill_body_prompt,
    load_skill_body_prompt,
    load_skill_metadata_prompt,
    read_skill_resource_text,
)
from ..services.llm_proxy import complete_chat_once
from ..services.skill_governance import allowed_skill_roots
from ..services.skill_manager import get_execution_skill_dir
from .chat import (
    _ALLOWED_PLAN_ACTIONS,
    _MAX_DEP_RETRY,
    _NODE_BUILTIN_MODULES,
    _PYTHON_HEREDOC_RE,
    _SCRIPT_INTERPRETERS,
    _blocks_for_planner,
    _expand_arg_env_vars,
    _extract_all_fenced_blocks,
    _extract_input_session_dir,
    _find_created_skill_roots,
    _get_skill_venv_python,
    _planner_model_name,
    _request_messages_with_files,
    _rewrite_argv_input_paths,
    _scan_and_install_node_deps,
    _scan_and_install_python_deps,
    _snapshot_dir_files,
    _strip_markdown_json_fence,
    _try_auto_install_interpreter,
    _validate_skill_md,
    _retry_install_node_dep,
    _retry_install_python_dep,
)
from .creator_chat import _has_creation_confirmation, _last_user_text
from .chat_models import ChatRequest, MarkdownBlock

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

def _allowed_skill_roots() -> list[Path]:
    """Return directories under which the executor may create or modify files."""
    roots = [root.expanduser().resolve() for root in allowed_skill_roots()]

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)

    return deduped

def _skill_root_for_name(skill_name: str) -> Path:
    """Resolve an existing sandbox skill root by skill_name."""
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise ValueError(f"非法 skill_name: {skill_name}")
    return get_execution_skill_dir(skill_name, mode="sandbox").resolve()

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

def _is_within_sandbox(entry: Path, sandbox_root: Path) -> bool:
    """Return True only when *entry* resolves to a path inside *sandbox_root*.

    Rejects symlinks that point outside the skill sandbox, preventing a
    malicious skill from exposing files such as /etc/passwd via read_resource.
    """
    try:
        entry.resolve().relative_to(sandbox_root)
        return True
    except ValueError:
        return False

def _looks_like_skill_resource_dir(path: Path) -> bool:
    return path.name in {"scripts", "references", "assets"}

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
        if path in seen:
            return
        seen.add(path)
        kind = path.split("/", 1)[0]
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
            "kind": item["kind"],
            "title": item.get("title", ""),
            "allowed_actions": item.get("allowed_actions", []),
            "usage_hint": item.get("usage_hint", ""),
        }
        for item in catalog
    ]

def _resource_catalog_by_handle(catalog: list[dict]) -> dict[str, dict]:
    return {str(item["resource_handle"]): item for item in catalog}

def _compose_resource_selection_prompt() -> str:
    return (
        "你是 Skill 资源按需加载选择器。\n\n"
        "你会看到 Loaded SKILL.md、resource_catalog 和用户请求。"
        "你的任务是判断当前阶段是否需要读取 references/assets/scripts 中的资源正文。\n\n"
        "重要规则：\n"
        "1. 只能从 resource_catalog 中选择 resource_handle。\n"
        "2. 禁止生成、拼接、改写资源 path。\n"
        "3. references 通常用于方法论、规范、示例，creator 生成 Skill 文件前应优先考虑。\n"
        "4. scripts 在 creator 阶段可以读取源码作为实现参考，但不要执行。\n"
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

    resource_by_handle = _resource_catalog_by_handle(resource_catalog)
    raw_handles = data.get("resource_handles", [])

    if not isinstance(raw_handles, list):
        raw_handles = []

    selected: list[str] = []
    for item in raw_handles:
        handle = str(item or "").strip()
        if not handle:
            continue
        if handle not in resource_by_handle:
            continue
        if handle not in selected:
            selected.append(handle)
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

def _compose_loaded_resources_prompt(
    *,
    skill_name: str,
    resource_catalog: list[dict],
    selected_handles: list[str],
) -> str:
    resource_by_handle = _resource_catalog_by_handle(resource_catalog)
    sections: list[str] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(handle)
        if not resource:
            continue

        path = resource["path"]
        try:
            observation = read_skill_resource_text(
                skill_name,
                path,
                max_chars=settings.skill_resource_max_chars,
            )
        except Exception as exc:
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error: {exc}\n"
            )
            continue

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

    if not sections:
        return ""

    return (
        "\n\n---\n\n"
        "## Loaded On-Demand Resources\n\n"
        "以下资源由宿主根据当前请求按需读取。"
        "这些内容现在可以作为执行当前 Skill 的依据。\n\n"
        + "\n\n".join(sections)
    )

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

def _compose_skill_runtime_planner_prompt() -> str:
    return (
        "你是 Skill Agent 运行时动作规划器。\n\n"
        "【重要】你只能输出一个严格的 JSON 对象，绝对不能输出任何自然语言、解释、思考过程或 Markdown 文本。"
        "你的全部输出必须是可直接被 json.loads() 解析的 JSON，不得有任何前缀或后缀。\n\n"
        "你的任务不是回答用户问题，而是根据 Loaded SKILL.md、resource_catalog、available_scripts 和用户请求，"
        "判断当前 Skill 应该直接回答，还是需要宿主执行结构化 action。\n\n"
        "核心原则：\n"
        "1. Loaded SKILL.md 是当前 Skill 的执行规范。\n"
        "2. resource_catalog 是宿主提供的真实资源树。\n"
        "3. available_scripts 是宿主从磁盘实时扫描到的真实脚本文件列表（权威来源）。"
        "available_scripts 中出现的脚本无需查 resource_catalog 即可直接规划 run_command。\n"
        "4. 你不能假设某个脚本存在；只能根据 available_scripts 或 resource_catalog 中真实出现的 scripts 资源规划 run_command。\n"
        "5. 你不能把函数名、伪代码函数、Python 函数、自然语言动作当成系统命令执行。\n"
        "6. 如果当前 Skill 是写作、故事生成、公文生成、报告生成、总结、翻译、润色、分析、咨询等语言生成类任务，"
        "且最终产物是纯文本或 Markdown（不是 .pptx/.xlsx/.docx 等格式文件），"
        "通常应使用 mode=direct_answer，不要规划 run_command。\n"
        "7. 如果 available_scripts 和 resource_catalog 均没有 scripts 资源，默认不得规划 run_command。\n"
        "8. 只有当 Loaded SKILL.md 明确要求运行外部命令，且该命令引用的脚本/资源确实存在于 available_scripts、"
        "resource_catalog 或系统可执行环境中，才允许规划 run_command。\n"
        "9. read_resource 只能使用 resource_handle，禁止输出 path。\n"
        "10. resource_handle 必须来自 resource_catalog。\n"
        "11. 如果任务需要 references/assets 的知识、示例、模板或配置，应优先规划 read_resource。\n"
        "12. 不要假装读取、假装执行、假装写入。\n"
        "13. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：\n"
        "- read_resource：读取 resource_catalog 中的资源，只能传 resource_handle。\n"
        "- run_command：执行一个真实可执行的命令。命令不得是函数名或伪代码。\n"
        "- write_file：写入文件。\n"
        "- create_directory：创建目录。\n"
        "- display / ignore：展示或忽略。\n\n"
        "文件生成任务强制规则（高优先级，覆盖规则 6）：\n"
        "当用户明确请求生成 PPT/PPTX/幻灯片、Excel/XLSX、Word/DOCX、CSV、图表图片、PDF 等可下载格式文件时：\n"
        "  a. 如果 available_scripts 或 resource_catalog 中存在可执行的 scripts 资源（如 build_pptx.js、read_excel.py 等），"
        "必须使用 mode=execute 并规划 run_command；不得使用 direct_answer。\n"
        "  b. 文本模型无法直接生成二进制文件（.pptx/.xlsx/.docx），必须通过执行脚本生成。\n"
        "  c. SKILL.md 中为文件生成任务指定了专用脚本时，stdin 字段应包含完整的输入内容（如幻灯片 JSON 数组）。\n\n"
        "mode 选择规则：\n"
        "- direct_answer：Skill 可由模型直接完成，且产物是纯文本/Markdown（不是格式化文件），例如写故事、公文、总结、翻译、分析。\n"
        "- execute：需要宿主执行 action，例如读取资源、运行脚本、写入文件，或生成 PPT/Excel 等格式文件。\n"
        "- ask_user：缺少必要输入，或 SKILL.md 要求的脚本/资源不存在，无法安全执行。\n"
        "- not_applicable：用户请求与当前 Skill 明显不匹配。\n\n"
        "run_command 约束：\n"
        "1. 不得输出类似 generate_story、process、main、run_task 这样的函数名作为 command。\n"
        "2. 不得凭空生成不在 available_scripts 中的 scripts/main.py、scripts/run.py 等路径。\n"
        "3. 如果 command 引用了 scripts/...，该路径必须能在 available_scripts 或 resource_catalog 中看到。\n"
        "4. 如果 available_scripts 和 resource_catalog 的 scripts 均为空，而任务又可由语言模型直接完成，应使用 direct_answer。\n"
        "5. 如果 Loaded SKILL.md 中只有示例命令，但对应脚本不在 available_scripts 中，应使用 ask_user，并在 errors 中说明脚本不存在。\n"
        "6. command 必须是完整的可执行命令行，包含脚本所需的所有参数，并用用户消息中的实际值替换 Loaded SKILL.md 里的占位符\n"
        "（例如 `<filepath>`、`{file}`、`<input>` 等）；不得在 command 中保留任何占位符或省略必要参数。\n"
        "7. 如果某个必要参数（例如文件路径、用户数据）在用户消息中未提供且无法从上下文推断，"
        "必须使用 ask_user 模式，并在 missing 列表中说明缺少哪些信息；不得用不完整的命令继续 execute。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"mode\": \"execute | direct_answer | ask_user | not_applicable\",\n"
        "  \"actions\": [\n"
        "    {\n"
        "      \"action\": \"read_resource\",\n"
        "      \"resource_handle\": \"resource:0\",\n"
        "      \"reason\": \"需要读取参考资料\"\n"
        "    },\n"
        "    {\n"
        "      \"action\": \"run_command\",\n"
        "      \"command\": \"scripts/process.py $INPUT_SESSION_DIR/data.xlsx --format markdown\",\n"
        "      \"stdin\": \"<可选：需要传给命令的标准输入>\",\n"
        "      \"reason\": \"需要运行真实存在的脚本或工具，命令包含从用户消息中提取的实际参数值\"\n"
        "    }\n"
        "  ],\n"
        "  \"final_instruction\": \"执行完成后优先基于 observation 回答；direct_answer 时按 Loaded SKILL.md 直接回答\",\n"
        "  \"missing\": [],\n"
        "  \"errors\": []\n"
        "}\n"
        "\n"
        "重要：如果用户上传了文件，命令中引用该文件时应使用环境变量路径。\n"
        "- Shell 脚本：`$INPUT_SESSION_DIR/<文件名>` 或 `$INPUT_DIR/<相对路径>`\n"
        "- Python 脚本：`os.environ['INPUT_SESSION_DIR'] + '/<文件名>'` 或 "
        "`os.path.join(os.environ['INPUT_DIR'], '<相对路径>')`\n"
        "不得使用 `uploads/`、`inputs/` 等相对路径，因为执行目录并非上传文件的存储位置。\n"
        "特别注意：Loaded SKILL.md 中的示例命令（例如 `uploads/data.xlsx`）只是占位符格式说明，"
        "其中的文件名（如 `data.xlsx`）并非真实文件名。\n"
        "必须从用户消息（user_messages 中的【已附上传文件：...】）中提取真实文件名，"
        "并以 `$INPUT_SESSION_DIR/<真实文件名>` 形式写入 command，不得保留 SKILL.md 中的示例文件名。\n"
    )

def _normalize_skill_runtime_plan(
    plan: dict,
    *,
    resource_catalog: list[dict] | None = None,
    execution_root: Path | None = None,
) -> dict:
    """Normalize planner JSON into executor-compatible plan.

    关键原则：
    - read_resource 的真实 path 不来自模型，而是由宿主根据 resource_handle 映射得到；
    - run_command 不允许凭空执行函数名或不存在的脚本；
    - command 只做通用可执行性校验，不硬编码 python/node/bash。
    """
    if not isinstance(plan, dict):
        raise ValueError("运行时规划模型输出必须是 JSON object")

    resource_by_handle = _resource_catalog_by_handle(resource_catalog or [])

    mode = str(plan.get("mode") or "").strip()
    if mode not in {"execute", "direct_answer", "ask_user", "not_applicable"}:
        mode = "ask_user"

    actions = plan.get("actions", [])
    errors = plan.get("errors", [])
    missing = plan.get("missing", [])

    if not isinstance(actions, list):
        actions = []

    if not isinstance(errors, list):
        errors = []

    if not isinstance(missing, list):
        missing = []

    normalized_actions: list[dict] = []

    for action_item in actions:
        if not isinstance(action_item, dict):
            continue

        action = str(action_item.get("action") or "").strip()

        if action not in {"run_command", "write_file", "create_directory", "read_resource", "display", "ignore"}:
            errors.append({"error": f"不支持的 action: {action}", "action_item": action_item})
            continue

        if action == "run_command":
            command = str(action_item.get("command") or "").strip()
            if not command:
                errors.append({"error": "run_command 缺少 command", "action_item": action_item})
                continue

            stdin_text = action_item.get("stdin", None)
            if stdin_text is not None:
                stdin_text = str(stdin_text)

            # 运行前预检：不执行，只验证命令形态和 Skill 内资源路径。
            try:
                _prepare_command_argv(command, base_dir=execution_root)
            except Exception as exc:
                errors.append({
                    "error": "run_command 预检失败",
                    "command": command,
                    "detail": str(exc),
                    "hint": (
                        "不要把函数名、伪代码或不存在的脚本当成命令。"
                        "如果当前 Skill 可直接由模型完成，请使用 mode=direct_answer。"
                    ),
                })
                continue

            action_item["command"] = command
            action_item["stdin"] = stdin_text

        elif action == "read_resource":
            resource_handle = str(action_item.get("resource_handle") or "").strip()
            if not resource_handle:
                errors.append({"error": "read_resource 缺少 resource_handle", "action_item": action_item})
                continue

            resource = resource_by_handle.get(resource_handle)
            if not resource:
                errors.append({
                    "error": "read_resource 使用了不存在的 resource_handle",
                    "resource_handle": resource_handle,
                    "available_resource_handles": sorted(resource_by_handle.keys()),
                })
                continue

            allowed_actions = set(resource.get("allowed_actions") or [])
            if "read_resource" not in allowed_actions:
                errors.append({
                    "error": "该资源不允许 read_resource",
                    "resource_handle": resource_handle,
                    "kind": resource.get("kind"),
                    "allowed_actions": sorted(allowed_actions),
                })
                continue

            action_item["resource_handle"] = resource_handle
            action_item["path"] = resource["path"]
            action_item["resource_kind"] = resource["kind"]

        elif action in {"write_file", "create_directory"}:
            path = str(action_item.get("path") or "").strip()
            if not path:
                errors.append({"error": f"{action} 缺少 path", "action_item": action_item})
                continue
            action_item["path"] = path

        if action == "write_file":
            if "content" not in action_item:
                errors.append({"error": "write_file 缺少 content", "action_item": action_item})
                continue
            action_item["content"] = str(action_item.get("content") or "")

        action_item["block_index"] = int(action_item.get("block_index", -1))
        normalized_actions.append(action_item)

    # 如果 planner 要 execute，但所有 action 都被宿主校验拦掉，
    # 不要继续进入 executor，改为 ask_user，让前端看到可解释错误。
    if mode == "execute" and not normalized_actions and errors:
        mode = "ask_user"

    return {
        "mode": mode,
        "tasks": normalized_actions,
        "actions": normalized_actions,
        "missing": missing,
        "errors": errors,
        "final_instruction": str(plan.get("final_instruction") or "").strip(),
    }

async def _run_skill_runtime_planner_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    execution_root: Path | None = None,
) -> dict:
    """Generate an action plan from Loaded SKILL.md and structured host resources.

    对齐反重力式宿主模型：
    - Skill.md 提供流程；
    - resource_catalog 提供资源树；
    - planner 只选择 resource_handle；
    - 真实 path 由宿主解析，不由模型生成。
    """
    resource_catalog = _extract_runtime_resource_catalog(body_prompt, execution_root=execution_root)
    planner_body_prompt = _strip_runtime_resource_manifest(body_prompt)

    # 扫描磁盘上真实存在的脚本文件，注入给 planner 以便直接规划 run_command
    available_scripts: list[str] = []
    if execution_root is not None:
        execution_root_resolved = execution_root.resolve()
        scripts_dir = execution_root_resolved / "scripts"
        if scripts_dir.is_dir() and _is_within_sandbox(scripts_dir, execution_root_resolved):
            available_scripts = sorted(
                "scripts/" + entry.name
                for entry in scripts_dir.iterdir()
                if entry.is_file()
                # Reject symlinks that escape the skill sandbox
                and _is_within_sandbox(entry, execution_root_resolved)
            )

    planner_payload = {
        "loaded_skill_prompt": planner_body_prompt,
        "resource_catalog": _resource_catalog_for_planner(resource_catalog),
        "available_scripts": available_scripts,
        "user_messages": _request_messages_with_files(request),
        "last_user_text": _last_user_text(request),
        "execution_root": str(execution_root) if execution_root else "",
        "runtime_contract": {
            "skill_md_is_markdown": True,
            "skill_md_code_blocks_have_no_action_tag": True,
            "resource_tree_is_structured": True,
            "planner_must_not_generate_resource_paths": True,
            "read_resource_uses_resource_handle_only": True,
            "resource_path_resolution_is_host_owned": True,
            "do_not_depend_on_main_model_markdown_output": True,
            "action_observation_loop": True,
        },
    }

    messages = [
        {"role": "system", "content": _compose_skill_runtime_planner_prompt()},
        {"role": "user", "content": json.dumps(planner_payload, ensure_ascii=False)},
    ]

    planner_model = _planner_model_name(model)
    planner_text = await complete_chat_once(messages, planner_model)

    try:
        stripped = _strip_markdown_json_fence(planner_text)
        raw_plan = json.loads(stripped)
    except json.JSONDecodeError:
        # First attempt failed.  Give the model one more chance with an explicit
        # correction prompt that reinforces the JSON-only requirement.
        logger.warning(
            "Planner returned non-JSON on first attempt, retrying with correction prompt: %s",
            planner_text[:300],
        )
        retry_messages = messages + [
            {"role": "assistant", "content": planner_text},
            {
                "role": "user",
                "content": (
                    "你的上一次回复包含了自然语言或 Markdown，不是合法的 JSON。\n"
                    "请重新输出，只输出一个符合格式要求的 JSON 对象，"
                    "不要任何解释、不要 Markdown、不要代码块标记。\n"
                    "直接输出 { ... }，不要其他内容。"
                ),
            },
        ]
        planner_text = await complete_chat_once(retry_messages, planner_model)
        try:
            stripped = _strip_markdown_json_fence(planner_text)
            raw_plan = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error(
                "Received invalid JSON response from skill runtime planner after retry: %s",
                planner_text,
            )
            raise ValueError(f"运行时规划模型没有返回合法 JSON: {planner_text[:500]}") from exc

    return await asyncio.to_thread(
        functools.partial(
            _normalize_skill_runtime_plan,
            raw_plan,
            resource_catalog=resource_catalog,
            execution_root=execution_root,
        )
    )

def _compose_final_answer_prompt() -> str:
    """Generate final answer from action observations."""
    return (
        "你是 Skill Agent 的最终回答生成器。\n\n"
        "你会收到用户请求、Loaded SKILL.md、运行时 action plan 和 executor observation。\n"
        "你必须基于 observation 回答用户，不要假装执行未发生的动作。\n"
        "如果命令执行成功，优先返回脚本 stdout 中的有效结果。\n"
        "如果命令执行失败，简要说明失败原因和 stderr/stdout 中的关键信息。\n"
        "如果 execution_result 中包含 output_files 列表（非空），必须在回答末尾以 Markdown 链接格式列出每个文件，"
        "格式示例：[下载 presentation.pptx](/api/skills/xxx/files/outputs/presentation.pptx)。\n"
        "不要输出内部 JSON，不要重复完整 SKILL.md，不要编造 observation 之外的执行结果。\n"
    )

async def _generate_final_answer_from_observation(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    plan: dict,
    execution_result: dict,
) -> str:
    messages = [
        {"role": "system", "content": _compose_final_answer_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "user_messages": _request_messages_with_files(request),
                    "plan": plan,
                    "execution_result": execution_result,
                },
                ensure_ascii=False,
            ),
        },
    ]

    return await complete_chat_once(messages, model)

def _compose_block_planner_prompt() -> str:
    return (
        "你是 Agent 运行时的动作规划器。\n\n"
        "你的唯一输入依据是：主模型已经生成的 assistant_text，以及从 assistant_text 中抽取出的 fenced code block。\n"
        "你不能根据 SKILL.md 模板、系统提示或用户原始意图凭空生成动作。\n\n"
        "核心规则：\n"
        "1. 只能判断 assistant_text 中已经出现的代码块。\n"
        "2. write_file 的文件内容必须来自对应 block 的 code，不能来自其他 block，不能来自解释文字。\n"
        "3. write_file 的 path 必须出现在该代码块紧邻前文中，通常应是代码块前最后 1 到 3 行里的“写入文件：<path>”或“保存到：<path>”。\n"
        "3a. 如果 assistant_text 中已经创建了某个 Skill 根目录，例如 `skills/ai-course-skill/scripts`、"
        "`skills/ai-course-skill/references` 或 `skills/ai-course-skill/assets`，"
        "那么后续写入 `SKILL.md` 必须绑定为 `skills/ai-course-skill/SKILL.md`，"
        "写入 `scripts/main.py` 必须绑定为 `skills/ai-course-skill/scripts/main.py`。\n"
        "3b. 禁止把新 Skill 的 `SKILL.md` 规划为宿主根目录下的 `SKILL.md`。\n"
        "3c. 禁止把新 Skill 的脚本规划为宿主根目录下的 `scripts/main.py`。\n"
        "4. 如果 path 出现在更早的段落、标题、列表或其他代码块附近，不允许把它绑定到当前 block。\n"
        "5. 如果当前 block 前后同时出现多个路径，或者路径与当前 block 内容主题明显不一致，不要猜测，写入 errors。\n"
        "6. 如果当前 block 的前文说写入 SKILL.md，但 block 内容明显是在描述其他文件、步骤、说明文字或另一个文件内容，不允许写入 SKILL.md。\n"
        "7. 如果当前 block 的前文说写入某个文件，但 block 内容明显不是该文件的完整内容，不允许写入该文件。\n"
        "8. 如果代码块表达的是创建目录，不要输出 run_command，必须输出 create_directory。\n"
        "9. 如果一个代码块中创建多个目录，必须拆成多个 create_directory 任务，每个任务一个 path。\n"
        "10. 对于修改宿主状态但宿主没有原生动作支持的操作，应优先 ignore，不要强行归类为 run_command。\n"
        "11. run_command 只用于确实需要运行外部程序、脚本或工具的命令，不要把目录创建、文件写入这类可由宿主原生动作完成的操作归类为 run_command。\n"
        "12. 如果代码块只是示例、说明、模板、教程、展示内容，则 action=display 或 ignore。\n"
        "13. 如果路径、执行意图、命令来源不明确，不要猜测，把问题写入 errors。\n"
        "14. 不允许根据用户希望、SKILL.md 用法、资源清单或常识补全缺失路径。\n"
        "15. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：display、ignore、write_file、run_command、create_directory。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"tasks\": [\n"
        "    {\"block_index\": 0, \"action\": \"create_directory\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 1, \"action\": \"write_file\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 2, \"action\": \"run_command\", \"command\": \"...\", \"reason\": \"...\"}\n"
        "  ],\n"
        "  \"errors\": []\n"
        "}\n"
    )

async def _run_block_planner_round(
        *,
        assistant_text: str,
        blocks: list[MarkdownBlock],
        request: ChatRequest,
        model: str,
) -> dict:
    """Run a silent planning round after the main model has produced assistant_text."""
    if not blocks:
        return {"tasks": [], "errors": []}

    planner_payload = {
        "user_messages": _request_messages_with_files(request),
        "assistant_text": assistant_text,
        "blocks": _blocks_for_planner(blocks),
        "runtime_constraints": {
            "block_source": "assistant_text_only",
            "path_source": "assistant_text_near_block_context",
            "content_source": "selected_block_code",
            "command_source": "assistant_text_executable_block_or_near_block_context",
            "directory_creation": {
                "preferred_action": "create_directory",
                "rule": "目录创建应使用 create_directory，不应使用 run_command。",
                "multiple_paths": "如果一次创建多个目录，拆成多个 create_directory 任务。",
            },
            "do_not_use": [
                "SKILL.md template",
                "system prompt",
                "resource manifest",
                "implicit intent",
                "guessed path",
                "guessed command",
            ],
        },
    }

    messages = [
        {"role": "system", "content": _compose_block_planner_prompt()},
        {"role": "user", "content": json.dumps(planner_payload, ensure_ascii=False)},
    ]

    planner_text = await complete_chat_once(messages, model)

    try:
        stripped = _strip_markdown_json_fence(planner_text)
        plan = json.loads(stripped)
    except json.JSONDecodeError as exc:
        logger.error("Received invalid JSON response from planner: %s", planner_text)
        raise ValueError(f"规划模型没有返回合法 JSON: {planner_text[:500]}") from exc

    if not isinstance(plan, dict):
        raise ValueError("规划模型输出必须是 JSON object")

    tasks = plan.get("tasks", [])
    errors = plan.get("errors", [])

    if not isinstance(tasks, list):
        raise ValueError("规划模型输出的 tasks 必须是数组")

    if not isinstance(errors, list):
        errors = []

    normalized_tasks: list[dict] = []

    for task in tasks:
        if not isinstance(task, dict):
            continue

        action = str(task.get("action", "")).strip()

        if action not in _ALLOWED_PLAN_ACTIONS:
            errors.append({"error": f"不支持的 action: {action}", "task": task})
            continue

        try:
            block_index = int(task.get("block_index", -1))
        except (TypeError, ValueError):
            block_index = -1

        if action in {"write_file", "run_command"} and not (0 <= block_index < len(blocks)):
            errors.append({"error": "任务缺少合法 block_index", "task": task})
            continue

        if action in {"write_file", "create_directory"} and not str(task.get("path") or "").strip():
            errors.append({"error": f"{action} 缺少 path", "task": task})
            continue

        if action == "run_command":
            block = blocks[block_index]
            command = str(task.get("command") or block.code or "").strip()
            if not command:
                errors.append({"error": "run_command 缺少 command", "task": task})
                continue
            task["command"] = command

        task["block_index"] = block_index
        normalized_tasks.append(task)

    return {"tasks": normalized_tasks, "errors": errors}

def _runtime_script_dir() -> Path:
    """Directory for executor-generated Python scripts converted from heredoc."""
    roots = _allowed_skill_roots()
    if not roots:
        raise ValueError("没有可用的 Skill 写入根目录")

    directory = roots[0] / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory

def _materialize_python_heredoc(command: str) -> list[str] | None:
    """Convert `python - <<'PY' ... PY` into `python <safe-script>.py`.

    目的：兼容模型常输出的多行校验脚本，同时继续使用 shell=False，
    不开放真正 shell 的管道、重定向、变量展开、命令替换等能力。
    """
    match = _PYTHON_HEREDOC_RE.match(command.strip())
    if not match:
        return None

    python_bin = Path(match.group("python")).name
    if python_bin not in {"python", "python3"}:
        raise ValueError(f"只允许运行 python/python3 heredoc 命令: {command}")

    script = match.group("script").rstrip() + "\n"
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
    script_path = _runtime_script_dir() / f"heredoc_{digest}.py"
    script_path.write_text(script, encoding="utf-8")

    resolved = _resolve_safe_path(str(script_path))
    return [python_bin, str(resolved)]

def _extract_skill_local_paths_from_argv(argv: list[str]) -> list[str]:
    """Extract skill-local resource paths mentioned in command argv.

    只识别 scripts/、references/、assets/ 这类 Skill 内资源路径。
    不关心具体语言，不硬编码 python/node/bash。
    """
    result: list[str] = []

    for raw in argv:
        if not raw:
            continue

        candidates = [raw]

        # 支持 --config=assets/config.yaml 这种形式
        if "=" in raw:
            _key, value = raw.split("=", 1)
            if value:
                candidates.append(value)

        for item in candidates:
            item = item.strip()
            if not item or item.startswith("-"):
                continue

            if item.startswith("./"):
                item = item[2:]

            try:
                path = Path(item)
            except Exception:
                continue

            parts = path.parts
            if not parts:
                continue

            if parts[0] in {"scripts", "references", "assets"}:
                normalized = Path(*parts).as_posix()
                if normalized not in result:
                    result.append(normalized)

    return result

def _validate_skill_local_command_paths(
    argv: list[str],
    *,
    base_dir: Path | None,
) -> None:
    """Validate skill-local paths referenced by a command.

    解决：
    - python scripts/main.py 但 scripts/main.py 不存在；
    - bash scripts/run.sh 但脚本不存在；
    - node scripts/index.js 但脚本不存在。

    这是资源存在性校验，不是工具类型白名单。
    """
    if base_dir is None:
        return

    root = base_dir.resolve()

    for rel_path in _extract_skill_local_paths_from_argv(argv):
        rel = Path(rel_path)

        if rel.is_absolute():
            raise ValueError(f"命令引用了非法绝对资源路径: {rel_path}")

        if any(part in {"", ".."} for part in rel.parts):
            raise ValueError(f"命令引用的资源路径越界: {rel_path}")

        resolved = (root / rel).resolve()

        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"命令引用的资源路径越界: {rel_path}") from exc

        if not resolved.exists():
            raise ValueError(f"命令引用的 Skill 资源不存在: {rel_path}")

        if not resolved.is_file():
            raise ValueError(f"命令引用的 Skill 资源不是文件: {rel_path}")

def _prepare_command_argv(
    command: str,
    *,
    base_dir: Path | None = None,
) -> list[str]:
    """Parse and preflight a command before subprocess.run.

    不限制具体执行工具类型；
    只做通用校验：
    - 命令不能为空；
    - 命令必须能被 shlex 解析；
    - argv[0] 必须是 PATH 中的可执行程序，或一个真实存在的路径；
    - command 中引用的 scripts/assets/references 路径必须真实存在。

    额外：对 Python 脚本使用每个 Skill 独立的 venv，执行前静态扫描依赖。
    对 Node.js 脚本执行前扫描并安装缺失的 npm 包。
    """
    argv = _safe_command_argv(command, base_dir=base_dir)

    if not argv:
        raise ValueError("命令为空")

    executable = argv[0]

    # 1. argv[0] 是路径形式：./tool、scripts/run.sh、/usr/bin/env 等
    if "/" in executable or "\\" in executable:
        exe_path = Path(executable).expanduser()

        if not exe_path.is_absolute():
            if base_dir is None:
                exe_path = exe_path.resolve()
            else:
                exe_path = (base_dir / exe_path).resolve()
        else:
            exe_path = exe_path.resolve()

        if not exe_path.exists():
            raise ValueError(f"命令不可执行，文件不存在: {executable}")

        if not exe_path.is_file():
            raise ValueError(f"命令不可执行，目标不是文件: {executable}")

        # 方案 A+B：对已知脚本扩展名（.py/.sh/.js 等）始终注入解释器，
        # 避免 execute bit 判断不一致导致的 PermissionError；
        # 对未知扩展名才依赖 execute bit 直接执行；
        # 若扩展名也无法识别，则给出明确错误提示。
        ext = exe_path.suffix.lower()
        if ext not in _SCRIPT_INTERPRETERS and os.access(exe_path, os.X_OK):
            # 非脚本文件且有执行权限，直接执行
            argv[0] = str(exe_path)
        else:
            interpreter = _SCRIPT_INTERPRETERS.get(ext)
            if interpreter is not None:
                # .ts 特殊处理：直接检查 ts-node 或通过 npx 运行
                if ext == ".ts":
                    if shutil.which("ts-node") is None:
                        _try_auto_install_interpreter("ts-node")
                    if shutil.which("ts-node") is not None:
                        argv = ["ts-node", str(exe_path)] + argv[1:]
                    elif shutil.which("npx") is not None:
                        argv = ["npx", "ts-node", str(exe_path)] + argv[1:]
                    else:
                        raise ValueError(
                            f"无法执行 {executable}：需要 ts-node 或 npx，但它们均不在 PATH 中。"
                        )
                elif ext == ".py" and base_dir is not None:
                    # 使用 Skill 独立 venv 执行 Python 脚本，并预装静态依赖
                    try:
                        venv_python = _get_skill_venv_python(base_dir)
                        _scan_and_install_python_deps(exe_path, venv_python)
                        argv = [str(venv_python), str(exe_path)] + argv[1:]
                    except Exception as venv_exc:
                        logger.warning(
                            "skill-env: venv setup failed, falling back to system python3: %s",
                            venv_exc,
                        )
                        if shutil.which("python3") is None:
                            _try_auto_install_interpreter("python3")
                        if shutil.which("python3") is None:
                            raise ValueError(
                                f"无法执行 {executable}：需要解释器 python3，但它不在 PATH 中。"
                            )
                        argv = ["python3", str(exe_path)] + argv[1:]
                elif ext in {".js", ".mjs", ".cjs"} and base_dir is not None:
                    # 预装 Node.js 依赖到 Skill 独立 node_modules
                    try:
                        _scan_and_install_node_deps(exe_path, base_dir)
                    except Exception as node_exc:
                        logger.warning("skill-env: node dep scan failed: %s", node_exc)
                    if shutil.which("node") is None:
                        _try_auto_install_interpreter("node")
                    if shutil.which("node") is None:
                        raise ValueError(
                            f"无法执行 {executable}：需要解释器 node，但它不在 PATH 中。"
                        )
                    argv = ["node", str(exe_path)] + argv[1:]
                else:
                    if shutil.which(interpreter) is None:
                        # 尝试自动安装后再检查一次
                        _try_auto_install_interpreter(interpreter)
                    if shutil.which(interpreter) is None:
                        raise ValueError(
                            f"无法执行 {executable}：需要解释器 {interpreter}，但它不在 PATH 中。"
                        )
                    argv = [interpreter, str(exe_path)] + argv[1:]
            else:
                raise ValueError(
                    f"命令没有执行权限: {executable}\n"
                    f"文件不可直接执行，且扩展名 '{ext or '(无)'}' 无法自动推断解释器。\n"
                    f"请使用 'node/python3/bash <脚本路径>' 的形式明确指定解释器。"
                )

    # 2. argv[0] 是裸命令：python、node、bash、ffmpeg、convert 等
    # 不做白名单，只检查系统 PATH 中是否存在。
    else:
        exe_name = Path(executable).name
        # 对裸 python/python3 + .py 脚本参数，替换为 Skill 独立 venv python
        if exe_name in {"python", "python3"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            script_path_candidate: Path | None = None
            if not script_arg.startswith("-") and (
                "/" in script_arg or script_arg.endswith(".py")
            ):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                # Guard: script must reside within the skill directory
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() == ".py":
                        script_path_candidate = candidate
                except ValueError:
                    pass  # path escaped skill dir boundary — skip dep scan
            if script_path_candidate is not None:
                try:
                    venv_python = _get_skill_venv_python(base_dir)
                    _scan_and_install_python_deps(script_path_candidate, venv_python)
                    argv = [str(venv_python)] + argv[1:]
                except Exception as venv_exc:
                    logger.warning(
                        "skill-env: venv setup failed, using system %s: %s",
                        executable,
                        venv_exc,
                    )
                    if shutil.which(executable) is None:
                        _try_auto_install_interpreter(executable)
            else:
                if shutil.which(executable) is None:
                    _try_auto_install_interpreter(executable)
        # 对裸 node/nodejs + .js 脚本参数，预装 Node.js 依赖
        elif exe_name in {"node", "nodejs"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            if not script_arg.startswith("-"):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                # Guard: script must reside within the skill directory
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() in {".js", ".mjs", ".cjs"}:
                        try:
                            _scan_and_install_node_deps(candidate, base_dir)
                        except Exception as node_exc:
                            logger.warning("skill-env: node dep scan failed: %s", node_exc)
                except ValueError:
                    pass  # path escaped skill dir boundary — skip dep scan
            if shutil.which(executable) is None:
                _try_auto_install_interpreter(executable)
        else:
            if shutil.which(executable) is None:
                # 尝试自动安装后再检查一次
                _try_auto_install_interpreter(executable)

        if shutil.which(executable) is None and not Path(argv[0]).exists():
            raise ValueError(
                f"命令不可执行：{executable} 不在 PATH 中，也不是当前 Skill 内的可执行文件。"
                "如果这是函数名或伪代码，请不要规划 run_command。"
            )

    _validate_skill_local_command_paths(argv, base_dir=base_dir)
    return argv

def _safe_command_argv(command: str, *, base_dir: Path | None = None) -> list[str]:
    """通用命令参数解析器。

    注意：
    - 不限制具体执行工具类型；
    - 不做 python/node/bash 白名单；
    - 真正的可执行性和资源存在性校验由 _prepare_command_argv 完成。
    """
    if not command or not command.strip():
        raise ValueError("命令为空")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令解析失败: {command}") from exc

    if not argv:
        raise ValueError("命令为空")

    return argv

def _execute_single_task(
    task: dict,
    blocks: "list[MarkdownBlock]",
    request: "ChatRequest",
    *,
    execution_root: "Path | None" = None,
    inferred_skill_root: "Path | None" = None,
    skill_name: str = "",
    session_input_dir: "Path | None" = None,
) -> "tuple[dict, list[Path]]":
    """Execute a single planned action task and return (result_dict, touched_paths).

    This is the per-task workhorse extracted from _execute_planned_actions so
    that callers (including the streaming execute loop in generate()) can run
    tasks one-at-a-time and observe results in real time.

    Returns:
        (result, touched) where *result* is the action result dict and
        *touched* is a (possibly empty) list of Path objects that were
        created or written during this task (used for post-loop validation).
    """
    if not isinstance(task, dict):
        return {}, []

    action = str(task.get("action") or "").strip()
    reason = str(task.get("reason") or "").strip()
    touched: list[Path] = []

    if action in {"display", "ignore"}:
        return {"action": action, "success": True, "reason": reason}, touched

    if action == "read_resource":
        rel_path = str(task.get("path") or "").strip()
        if not rel_path:
            raise ValueError("read_resource 任务缺少 path")
        if not skill_name:
            raise ValueError("read_resource 任务缺少 skill_name，无法确定读取哪个 Skill 的资源")
        observation = read_skill_resource_text(
            skill_name, rel_path, max_chars=settings.skill_resource_max_chars
        )
        return {
            "action": action,
            "path": rel_path,
            "success": True,
            "content": observation.get("content", ""),
            "truncated": observation.get("truncated", False),
            "reason": reason,
        }, touched

    if action == "create_directory":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("create_directory 任务缺少 path")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.mkdir(parents=True, exist_ok=True)
        touched.append(path)
        return {"action": action, "path": str(path), "success": True, "reason": reason}, touched

    if action == "write_file":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("write_file 任务缺少 path")
        content = task.get("content", None)
        if content is None:
            block_index = int(task.get("block_index", -1))
            if 0 <= block_index < len(blocks):
                content = blocks[block_index].code
            else:
                raise ValueError("write_file 任务缺少 content，且没有合法 block_index")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        touched.append(path)
        return {
            "action": action,
            "path": str(path),
            "success": True,
            "bytes": len(str(content).encode("utf-8")),
            "reason": reason,
        }, touched

    if action == "run_command":
        command = str(task.get("command") or "").strip()
        if not command:
            raise ValueError("run_command 任务缺少 command")

        stdin_text = task.get("stdin", None)
        if stdin_text is not None:
            stdin_text = str(stdin_text)

        cwd = execution_root or inferred_skill_root

        # Per-task snapshot taken *before* execution to detect new output files.
        pre_snapshot: set[str] = _snapshot_dir_files(cwd) if cwd else set()

        materialized = _materialize_python_heredoc(command)
        if materialized is not None:
            argv = materialized
            argv = _prepare_command_argv(
                " ".join(shlex.quote(part) for part in argv), base_dir=cwd
            )
        else:
            argv = _prepare_command_argv(command, base_dir=cwd)

        argv = _rewrite_argv_input_paths(
            argv,
            getattr(request, "input_files", []) or [],
            cwd,
            session_input_dir,
        )

        _run_cmd_extra_env: dict[str, str] = {
            "OUTPUT_DIR": str(cwd / "outputs") if cwd else "",
            "INPUT_DIR": str(cwd / "inputs") if cwd else "",
        }
        if session_input_dir is not None:
            _run_cmd_extra_env["INPUT_SESSION_DIR"] = str(session_input_dir)

        _effective_env = {**os.environ, **_run_cmd_extra_env}
        argv = [_expand_arg_env_vars(arg, _effective_env) for arg in argv]

        # Error-driven retry: up to _MAX_DEP_RETRY times for missing deps.
        completed = None
        for _retry in range(_MAX_DEP_RETRY + 1):
            try:
                completed = subprocess.run(
                    argv,
                    shell=False,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    timeout=settings.skill_command_timeout,
                    cwd=str(cwd) if cwd else None,
                    env={**os.environ, **_run_cmd_extra_env},
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    "命令不可执行: " + command + "\n原因: " + str(exc)
                ) from exc
            except PermissionError as exc:
                raise ValueError(
                    "命令没有执行权限: " + command + "\n原因: " + str(exc)
                ) from exc

            if completed.returncode == 0 or _retry == _MAX_DEP_RETRY:
                break

            stderr = completed.stderr or ""
            retried = False

            py_missing = re.search(
                r"ModuleNotFoundError: No module named '([^']+)'", stderr
            )
            if py_missing and cwd is not None:
                module_name = py_missing.group(1).split(".")[0]
                try:
                    venv_python = _get_skill_venv_python(cwd)
                    if _retry_install_python_dep(module_name, venv_python):
                        retried = True
                except Exception as dep_exc:
                    logger.warning(
                        "skill-env: error-driven py dep install failed: %s", dep_exc
                    )

            node_missing = re.search(r"Cannot find module '([^']+)'", stderr)
            if node_missing and cwd is not None:
                raw_mod = node_missing.group(1)
                if raw_mod.startswith("@"):
                    parts = raw_mod.split("/")
                    module_name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                else:
                    module_name = raw_mod.split("/")[0]
                if module_name not in _NODE_BUILTIN_MODULES:
                    if _retry_install_node_dep(module_name, cwd):
                        retried = True

            if not retried and cwd is not None:
                chinese_missing = re.search(
                    r"缺少依赖[:：]\s*([^\n]+)",
                    (completed.stdout or "") + "\n" + stderr,
                )
                if chinese_missing:
                    raw_deps = chinese_missing.group(1)
                    pkg_list = [
                        p.strip()
                        for p in re.split(r"[,，、;；]\s*", raw_deps)
                        if p.strip()
                    ]
                    for dep in pkg_list:
                        if dep in _NODE_BUILTIN_MODULES:
                            continue
                        if (
                            dep.endswith(".js")
                            or (cwd / "node_modules").is_dir()
                            or shutil.which("node")
                        ):
                            if _retry_install_node_dep(dep, cwd):
                                retried = True
                        else:
                            try:
                                venv_python = _get_skill_venv_python(cwd)
                                if _retry_install_python_dep(dep, venv_python):
                                    retried = True
                            except Exception as dep_exc:
                                logger.warning(
                                    "skill-env: chinese dep install failed: %s", dep_exc
                                )

            if not retried:
                break

        assert completed is not None  # noqa: S101 — loop always runs at least once (range >= 1)
        success = completed.returncode == 0

        result: dict = {
            "action": action,
            "command": command,
            "stdin_used": stdin_text is not None,
            "success": success,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "reason": reason,
        }

        # Detect newly created files and attach download metadata.
        effective_skill_name = skill_name or (cwd.name if cwd else "")
        if success and cwd and effective_skill_name:
            post_snapshot = _snapshot_dir_files(cwd)
            new_files = sorted(post_snapshot - pre_snapshot)
            if new_files:
                result["output_files"] = [
                    {
                        "path": f,
                        "url": f"/api/skills/{effective_skill_name}/files/{f}",
                    }
                    for f in new_files
                ]

        return result, touched

    raise ValueError(f"不支持的规划动作: {action}")

def _execute_planned_actions(
    plan: dict,
    blocks: list[MarkdownBlock],
    request: ChatRequest,
    *,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """执行结构化 action plan，并返回 executor observation。"""
    if require_confirmation and not _has_creation_confirmation(request):
        return {
            "executed": False,
            "reason": "未检测到用户明确确认开始创建，因此不会执行规划任务。",
            "plan": plan,
            "results": [],
            "logs": [],
        }

    inferred_skill_root = _infer_skill_root_from_tasks(
        plan,
        execution_root=execution_root,
    )

    # Pre-compute session input dir once (used for all run_command tasks).
    cwd_for_session = execution_root or inferred_skill_root
    session_input_dir = _extract_input_session_dir(
        getattr(request, "input_files", []) or [], cwd_for_session
    )

    touched: list[Path] = []
    results: list[dict] = []
    logs: list[str] = []

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()

        result, task_touched = _execute_single_task(
            task,
            blocks,
            request,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
            skill_name=skill_name,
            session_input_dir=session_input_dir,
        )

        touched.extend(task_touched)
        results.append(result)

        # Build logs from the result dict.
        if action == "read_resource":
            logs.append(f"读取资源成功: {result.get('path')}")
        elif action == "create_directory":
            logs.append(f"创建目录: {result.get('path')}")
        elif action == "write_file":
            logs.append(f"写入文件: {result.get('path')}")
        elif action == "run_command":
            command = str(task.get("command") or "").strip()
            stdin_used = result.get("stdin_used", False)
            if result.get("output_files"):
                logs.append(
                    "新生成文件: " + ", ".join(f["path"] for f in result["output_files"])
                )
            if not result.get("success", True):
                logs.append(
                    f"执行命令失败: {command}\n"
                    f"returncode={result.get('returncode')}\n"
                    f"stdin_used={stdin_used}\n"
                    f"stderr: {(result.get('stderr') or '').strip()}\n"
                    f"stdout: {(result.get('stdout') or '').strip()}"
                )
            else:
                logs.append(
                    f"执行命令成功: {command}\n"
                    f"stdin_used={stdin_used}\n"
                    f"输出: {(result.get('stdout') or '').strip()}"
                )

    validation_logs: list[str] = []

    for root in _find_created_skill_roots(touched):
        skill_md = root / "SKILL.md"
        if skill_md.exists():
            _validate_skill_md(skill_md)
            validation_logs.append(f"校验通过: {skill_md}")

    logs.extend(validation_logs)

    # 汇总所有 run_command 任务产生的新文件
    all_output_files: list[dict] = []
    for r in results:
        all_output_files.extend(r.get("output_files") or [])

    return {
        "executed": bool(results or touched),
        "reason": "已根据结构化 action plan 执行任务。" if (results or touched) else "规划中没有需要执行的任务。",
        "plan": plan,
        "results": results,
        "logs": logs,
        "output_files": all_output_files,
    }

# 兼容保留：旧的 bash-block 执行器。不再作为主路径使用。

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

async def _plan_and_execute_generated_output(
    *,
    assistant_text: str,
    request: ChatRequest,
    model: str,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """Legacy fallback: plan and execute actions from main model Markdown output.

    新主路径不再依赖这个函数。
    仅当 runtime planner 判断 direct_answer，或者旧 Skill 仍要求通过主模型 Markdown 输出动作时，才作为兜底。
    """
    blocks = _extract_all_fenced_blocks(assistant_text)

    if not blocks:
        return {
            "executed": False,
            "reason": "主模型回复中未检测到 fenced code block。",
            "plan": {"tasks": [], "errors": []},
            "results": [],
        }

    planner_model = _planner_model_name(model)

    plan = await _run_block_planner_round(
        assistant_text=assistant_text,
        blocks=blocks,
        request=request,
        model=planner_model,
    )

    if plan.get("errors") and not plan.get("tasks"):
        return {
            "executed": False,
            "reason": "规划模型未生成可执行任务。",
            "plan": plan,
            "results": [],
        }

    return await asyncio.to_thread(
        functools.partial(
            _execute_planned_actions,
            plan,
            blocks,
            request,
            require_confirmation=require_confirmation,
            execution_root=execution_root,
            skill_name=skill_name,
        )
    )
def build_skill_context(skill_name: str) -> dict:
    """Build sandbox skill context for an existing skill."""
    skill_root = _skill_root_for_name(skill_name)
    skill_metadata_prompt = load_skill_metadata_prompt(skill_name)

    return {
        "skill_name": skill_name,
        "metadata_prompt": skill_metadata_prompt,
        "body_loader": lambda: load_skill_body_prompt(skill_name),
        "child_body_loader": lambda child_ref: load_child_skill_body_prompt(skill_name, child_ref),
        "force_body": False,
        "enable_action_execution": True,
        "require_action_confirmation": False,
        "execution_root": skill_root,
        "strict_skill_execution": True,
        "enable_resource_preload": True,
    }


@router.post("/sandbox/{skill_name}")
async def chat_in_sandbox(skill_name: str, request: ChatRequest):
    """Multi-turn chat with a specific skill loaded in sandbox mode."""
    from .chat import _make_stream  # local import avoids circular dependency

    try:
        skill_context = build_skill_context(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return _make_stream(skill_context, request)
