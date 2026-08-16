"""运行时规划器。"""

import asyncio
import functools
import json
import logging
from pathlib import Path

from ...services.llm_proxy import complete_chat_once
from ..chat_utils import (
    _request_messages_with_files,
    _last_user_text,
    _planner_model_name,
    _strip_markdown_json_fence,
    _is_within_sandbox,
)
from ..chat_models import ChatRequest
from .path_resolution import (
    _normalize_skill_resource_path,
    _available_scripts_for_root,
)
from .resource_catalog import (
    _extract_runtime_resource_catalog,
    _resource_catalog_for_planner,
    _resource_catalog_by_handle,
    _resolve_resource_handle_alias,
)
from .action_schema import _extract_skill_command_contract
from .workflow_detection import (
    _should_force_skill_workflow,
    _final_instruction_requests_host_command,
)

logger = logging.getLogger(__name__)


def _compose_skill_runtime_planner_prompt() -> str:
    return (
        "你是 Skill Agent 的合同驱动运行时规划器。只输出可被 json.loads 解析的 JSON object。\n"
        "根据 loaded_skill_prompt、input_envelope、resource_catalog、available_scripts、"
        "action_schema、execution_root、loaded_resources、failed_resources 和当前用户要求规划。\n"
        "不得根据文件名、扩展名或业务关键词推断 workflow；只依据 Skill 合同、Action schema、"
        "输入可访问性和步骤数据依赖。不得假装读取或执行。\n"
        "mode 规则：direct_answer 仅当合同允许模型完成、必要输入可访问且无需宿主动作；"
        "execute 用于 read_resource/display/ignore 或现有单阶段宿主动作；"
        "execute_workflow 用于 Action schema 的执行责任、步骤数据依赖或必须由 executable workflow 产生结果；"
        "ask_user 仅当执行必需输入无法从 envelope、resources、默认值或前序结果获得；"
        "not_applicable 表示合同确实不适用。\n"
        "read_resource 只能使用 resource_catalog 内的 resource_handle。不得发明命令或路径。\n"
        "输出：{\"mode\":\"execute_workflow|execute|direct_answer|ask_user|not_applicable\","
        "\"actions\":[{\"action\":\"read_resource|display|ignore\",\"resource_handle\":\"resource:0\",\"reason\":\"...\"}],"
        "\"missing\":[],\"errors\":[],\"final_instruction\":\"...\"}"
    )


def _normalize_skill_runtime_plan(
    plan: dict,
    *,
    resource_catalog: list[dict] | None = None,
    execution_root: Path | None = None,
    command_contract: dict | None = None,
    loaded_paths: list[str] | None = None,
    failed_paths: list[dict] | None = None,
    available_scripts: list[str] | None = None,
    user_text: str = "",
) -> dict:
    """Normalize planner JSON into executor-compatible plan.

    关键原则：
    - read_resource 的真实 path 不来自模型，而是由宿主根据 resource_handle 映射得到；
    - runtime planner 不直接触发 run_command/write_file/create_directory；
    - 命令和写文件只能由后续主模型回复中的 fenced code block 触发。
    """
    if not isinstance(plan, dict):
        raise ValueError("运行时规划模型输出必须是 JSON object")

    resource_by_handle = _resource_catalog_by_handle(resource_catalog or [])
    loaded_path_set = {_normalize_skill_resource_path(path) for path in (loaded_paths or []) if str(path or "").strip()}
    failed_by_path: dict[str, dict] = {}
    for item in failed_paths or []:
        if not isinstance(item, dict):
            continue
        failed_path = _normalize_skill_resource_path(str(item.get("path") or ""))
        if failed_path:
            failed_by_path[failed_path] = item
    available_script_set = {_normalize_skill_resource_path(path) for path in (available_scripts or [])}
    command_entries = (((command_contract or {}).get("action_schema") or {}).get("entries") or [])
    command_script_set = {_normalize_skill_resource_path(str(entry.get("script_path") or "")) for entry in command_entries if isinstance(entry, dict)}

    mode = str(plan.get("mode") or "").strip()
    if mode not in {"execute", "execute_workflow", "direct_answer", "ask_user", "not_applicable", "plan"}:
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

    planner_inconsistent: list[dict] = []
    normalized_missing: list[dict] = []
    for missing_item in missing:
        if not isinstance(missing_item, dict):
            normalized_missing.append({"missing_type": "planner_inconsistent", "reason": str(missing_item)})
            continue
        normalized_item = dict(missing_item)
        rel_path = _normalize_skill_resource_path(str(normalized_item.get("path") or ""))
        resource_handle = str(normalized_item.get("resource_handle") or "").strip()
        if resource_handle:
            canonical_handle, resolved_resource, was_alias = _resolve_resource_handle_alias(resource_handle, resource_catalog or [])
            if canonical_handle and resolved_resource:
                if was_alias:
                    planner_inconsistent.append({
                        "missing_type": "planner_inconsistent",
                        "resource_handle": resource_handle,
                        "resolved_resource_handle": canonical_handle,
                        "path": _normalize_skill_resource_path(str(resolved_resource.get("path") or "")),
                        "reason": "planner used a path-like pseudo resource_handle; backend resolved it from resource_catalog",
                    })
                resource_handle = canonical_handle
                normalized_item["resource_handle"] = canonical_handle
                if not rel_path:
                    rel_path = _normalize_skill_resource_path(str(resolved_resource.get("path") or ""))
                    normalized_item["path"] = rel_path

        if rel_path in available_script_set:
            planner_inconsistent.append({
                "missing_type": "planner_inconsistent",
                "resource_handle": resource_handle,
                "path": rel_path,
                "reason": "planner reported a script as missing, but backend available_scripts shows it exists",
            })
            continue

        if rel_path in loaded_path_set:
            planner_inconsistent.append({
                "missing_type": "planner_inconsistent",
                "resource_handle": resource_handle,
                "path": rel_path,
                "reason": "planner reported an already loaded resource as missing",
            })
            continue

        if rel_path.startswith("scripts/") and rel_path not in command_script_set and (command_contract or {}).get("has_executable_command_block") is False:
            normalized_item["missing_type"] = "command_block_missing"
            normalized_item["reason"] = normalized_item.get("reason") or "script exists/mentioned but SKILL.md/references has no executable command block"

        if rel_path in failed_by_path:
            failed = failed_by_path[rel_path]
            normalized_item["missing_type"] = failed.get("missing_type") or "load_failed"
            normalized_item["reason"] = failed.get("reason") or normalized_item.get("reason") or "resource load failed"
        else:
            normalized_item["missing_type"] = normalized_item.get("missing_type") or "file_missing"
        normalized_missing.append(normalized_item)
    missing = normalized_missing

    normalized_actions: list[dict] = []

    for action_item in actions:
        if not isinstance(action_item, dict):
            continue

        action = str(action_item.get("action") or "").strip()

        if action not in {"run_command", "write_file", "create_directory", "read_resource", "display", "ignore"}:
            errors.append({"error": f"不支持的 action: {action}", "action_item": action_item})
            continue

        if action in {"run_command", "write_file", "create_directory"}:
            errors.append({
                "error": f"{action} 只能由主模型回复中的显式 fenced code block 触发",
                "action_item": action_item,
                "hint": "runtime planner 只做意图判断和 read_resource；不要直接规划执行命令或写文件。",
            })
            continue

        if action == "read_resource":
            resource_handle = str(action_item.get("resource_handle") or "").strip()
            if not resource_handle:
                errors.append({"error": "read_resource 缺少 resource_handle", "action_item": action_item})
                continue

            canonical_handle, resource, was_alias = _resolve_resource_handle_alias(resource_handle, resource_catalog or [])
            if not resource or not canonical_handle:
                errors.append({
                    "error": "read_resource 使用了不存在的 resource_handle",
                    "resource_handle": resource_handle,
                    "reason": "planner_inconsistent",
                    "available_resource_handles": sorted(resource_by_handle.keys()),
                })
                continue
            if was_alias:
                planner_inconsistent.append({
                    "missing_type": "planner_inconsistent",
                    "resource_handle": resource_handle,
                    "resolved_resource_handle": canonical_handle,
                    "path": _normalize_skill_resource_path(str(resource.get("path") or "")),
                    "reason": "planner used a path-like pseudo resource_handle; backend resolved it from resource_catalog",
                })
                resource_handle = canonical_handle

            rel_path = _normalize_skill_resource_path(str(resource.get("path") or ""))
            if rel_path in failed_by_path:
                failed = failed_by_path[rel_path]
                missing.append({
                    "resource_handle": resource_handle,
                    "path": rel_path,
                    "missing_type": failed.get("missing_type") or "load_failed",
                    "reason": failed.get("reason") or "resource load failed",
                })
                continue

            if execution_root is not None:
                root = execution_root.resolve()
                resource_path = (root / rel_path).resolve()
                if not _is_within_sandbox(resource_path, root) or not resource_path.is_file():
                    missing.append({
                        "resource_handle": resource_handle,
                        "path": rel_path,
                        "missing_type": "file_missing",
                        "reason": "resource_catalog entry no longer exists in current skill",
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


        action_item["block_index"] = int(action_item.get("block_index", -1))
        normalized_actions.append(action_item)

    workflow_reason = _should_force_skill_workflow(
        command_contract=command_contract or {},
        user_text=user_text,
    )
    if workflow_reason:
        mode = "execute_workflow"
        normalized_actions = [item for item in normalized_actions if str(item.get("action") or "") == "read_resource"]
        planner_inconsistent.append({
            "missing_type": "workflow_forced",
            "reason": workflow_reason,
        })

    # 如果 planner 要 execute，但所有 action 都被宿主校验拦掉，
    # 不要继续进入 executor，改为 ask_user，让前端看到可解释错误。
    if mode == "execute" and not normalized_actions and errors:
        mode = "ask_user"

    if mode == "ask_user" and planner_inconsistent and not missing and not errors:
        mode = "direct_answer"

    final_instruction = str(plan.get("final_instruction") or "").strip()
    if (
        mode == "direct_answer"
        and _final_instruction_requests_host_command(final_instruction)
        and not (command_contract or {}).get("has_executable_command_block")
    ):
        mode = "ask_user"
        errors.append({
            "error": "Skill.md 缺少可执行命令 fenced block 示例，禁止主模型临时拼接命令",
            "hint": "请在当前 SKILL.md 中用普通 Markdown 写入具体 ```bash 命令示例，并让脚本接口与示例一致。",
        })

    workflow_actions = []
    if mode == "execute_workflow":
        for entry in command_entries:
            if not isinstance(entry, dict):
                continue
            script_path = _normalize_skill_resource_path(str(entry.get("script_path") or ""))
            if not script_path.startswith("scripts/"):
                continue
            workflow_actions.append({
                "action": "run_command",
                "script_path": script_path,
                "command_template": str(entry.get("command") or ""),
                "reason": "execute_workflow Action schema step",
            })

    return {
        "mode": mode,
        "tasks": normalized_actions,
        "actions": normalized_actions,
        "workflow_actions": workflow_actions,
        "missing": missing,
        "errors": errors,
        "planner_inconsistent": planner_inconsistent,
        "final_instruction": final_instruction,
        "command_contract": command_contract or {},
    }

async def _run_skill_runtime_planner_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    execution_root: Path | None = None,
    skill_name: str = "",
    loaded_paths: list[str] | None = None,
    failed_paths: list[dict] | None = None,
) -> dict:
    """Generate an action plan from Loaded SKILL.md and structured host resources.

    对齐反重力式宿主模型：
    - Skill.md 提供流程；
    - resource_catalog 提供资源树；
    - planner 只选择 resource_handle；
    - 真实 path 由宿主解析，不由模型生成。
    """
    from .multimodal import _strip_runtime_resource_manifest
    resource_catalog = _extract_runtime_resource_catalog(body_prompt, execution_root=execution_root)
    planner_body_prompt = _strip_runtime_resource_manifest(body_prompt)
    command_contract = _extract_skill_command_contract(planner_body_prompt, execution_root=execution_root)
    from .io_manifest import build_input_envelope
    input_envelope = build_input_envelope(request, execution_root)

    # Deterministically scan only the current business Skill root. Never scan kernel.
    available_scripts = _available_scripts_for_root(execution_root)
    logger.info(
        "sandbox runtime planner context skill_name=%s execution_root=%s available_scripts=%s",
        skill_name,
        str(execution_root.resolve()) if execution_root else "",
        available_scripts,
    )

    planner_payload = {
        "loaded_skill_prompt": planner_body_prompt,
        "input_envelope": input_envelope,
        "resource_catalog": _resource_catalog_for_planner(resource_catalog),
        "available_scripts": available_scripts,
        "action_schema": command_contract,
        "user_messages": _request_messages_with_files(request),
        "last_user_text": _last_user_text(request),
        "execution_root": str(execution_root) if execution_root else "",
        "skill_name": skill_name,
        "loaded_resources": list(loaded_paths or []),
        "failed_resources": list(failed_paths or []),
        "runtime_contract": {
            "skill_md_is_markdown": True,
            "skill_md_code_blocks_have_no_action_tag": True,
            "resource_tree_is_structured": True,
            "planner_must_not_generate_resource_paths": True,
            "read_resource_uses_resource_handle_only": True,
            "resource_path_resolution_is_host_owned": True,
            "execution_requires_main_model_fenced_block": False,
            "multi_script_skills_use_execute_workflow": True,
            "action_observation_loop": True,
            "command_generation_requires_skill_md_markdown_example": True,
            "fenced_blocks_are_normalized_to_action_schema": True,
            "reference_command_blocks_are_valid_execution_entries": True,
            "stdout_json_is_observation_for_final_answer": True,
        },
    }

    messages = [
        {"role": "system", "content": _compose_skill_runtime_planner_prompt()},
        {"role": "user", "content": f"## Skill 执行规范\n{planner_body_prompt}"},
        {"role": "user", "content": f"## 可用脚本\n{json.dumps(available_scripts, ensure_ascii=False)}"},
        {"role": "user", "content": f"## SKILL.md / references Action schema\n{json.dumps(command_contract, ensure_ascii=False)}"},
        {"role": "user", "content": f"## Input Envelope\n{json.dumps(input_envelope, ensure_ascii=False)}"},
        {"role": "user", "content": f"## 执行根目录\n{str(execution_root) if execution_root else ''}"},
        {"role": "user", "content": f"## 技能名称\n{skill_name}"},
        {"role": "user", "content": "## 已加载/加载失败资源\n" + json.dumps({"loaded_paths": list(loaded_paths or []), "failed_paths": list(failed_paths or [])}, ensure_ascii=False)},
        {"role": "user", "content": "请根据以上信息，输出 JSON 格式的执行计划。只输出 JSON，不要任何其他内容。"},
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
            command_contract=command_contract,
            loaded_paths=loaded_paths,
            failed_paths=failed_paths,
            available_scripts=available_scripts,
            user_text=_last_user_text(request),
        )
    )


# Public aliases
normalize_skill_runtime_plan = _normalize_skill_runtime_plan
compose_skill_runtime_planner_prompt = _compose_skill_runtime_planner_prompt


# ---------------------------------------------------------------------------
# 补充规划：执行后判断是否需要更多任务
# ---------------------------------------------------------------------------

_SUPPLEMENTARY_PLANNER_PROMPT = """\
你是 Skill Agent 补充规划判断器。

当前已有一批任务执行完毕，你需要判断执行结果是否已足够生成用户所需的最终答案。

核心原则：
1. 如果执行结果已经包含了用户请求所需的所有信息，need_more=false。
2. 如果执行结果缺少关键信息（如缺少某个资源的读取、某个脚本的执行），need_more=true 并提供补充任务。
3. 补充任务只能是 read_resource 或 run_command，且必须引用当前 Skill 中已声明的脚本和资源。
4. 不要为了"更完善"而补充不必要的任务。

输出格式（严格 JSON，不要 Markdown）：
{
  "need_more": true | false,
  "reason": "简短判断理由",
  "additional_tasks": [
    {"action": "read_resource", "resource_handle": "resource:0", "reason": "为什么需要"}
  ] | null,
  "final_answer_hint": "如果 need_more=false，可提供答案生成提示"
}
"""


async def _run_supplementary_plan_round(
    *,
    body_prompt: str,
    user_text: str,
    execution_results: list[dict],
    loaded_resources: list[str],
    failed_resources: list[str],
    model: str,
    execution_root: Path | None = None,
    resource_catalog: list[dict] | None = None,
) -> dict:
    """根据已执行结果判断是否需要补充执行。

    Returns:
        {
            "need_more": bool,
            "reason": str,
            "additional_tasks": list[dict] | None,
            "final_answer_hint": str,
        }
    """
    # 构建执行结果摘要（截断避免 token 溢出）
    result_summaries: list[str] = []
    for r in execution_results:
        action = r.get("action", "unknown")
        success = r.get("success", True)
        stdout = (r.get("stdout", "") or "")[:500]
        stderr = (r.get("stderr", "") or "")[:200]
        result_summaries.append(
            f"- {action} (success={success}): stdout={stdout}"
            + (f" stderr={stderr}" if stderr else "")
        )

    summary_text = "\n".join(result_summaries) if result_summaries else "(无执行结果)"

    # 构建 resource_catalog 摘要
    from .resource_catalog import _resource_catalog_for_planner
    catalog_summary = _resource_catalog_for_planner(resource_catalog or [])

    messages = [
        {"role": "system", "content": _SUPPLEMENTARY_PLANNER_PROMPT},
        {"role": "user", "content": f"## Skill 执行规范\n{body_prompt[:6000]}"},
        {"role": "user", "content": f"## 用户请求\n{user_text}"},
        {"role": "user", "content": f"## 已执行任务及结果摘要\n{summary_text}"},
        {"role": "user", "content": f"## 已加载资源\n{json.dumps(loaded_resources, ensure_ascii=False)}"},
        {"role": "user", "content": f"## 加载失败资源\n{json.dumps(failed_resources, ensure_ascii=False)}"},
        {"role": "user", "content": f"## 可用资源目录\n{json.dumps(catalog_summary, ensure_ascii=False)[:2000]}"},
        {"role": "user", "content": "请判断当前信息是否充足，输出严格 JSON。"},
    ]

    planner_model = _planner_model_name(model)
    response_text = await complete_chat_once(messages, planner_model)

    try:
        stripped = _strip_markdown_json_fence(response_text)
        plan = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("Supplementary planner returned non-JSON: %s", response_text[:300])
        return {
            "need_more": False,
            "reason": "无法解析补充规划结果，直接生成最终答案",
            "additional_tasks": None,
            "final_answer_hint": "",
        }

    if not isinstance(plan, dict):
        return {
            "need_more": False,
            "reason": "补充规划结果不是 JSON object",
            "additional_tasks": None,
            "final_answer_hint": "",
        }

    # 安全过滤：只允许白名单动作类型
    allowed_actions = {"read_resource", "run_command", "display", "ignore"}
    tasks = plan.get("additional_tasks") or []
    filtered = [
        t for t in tasks
        if isinstance(t, dict) and str(t.get("action") or "") in allowed_actions
    ]
    plan["additional_tasks"] = filtered if filtered else None

    return plan
