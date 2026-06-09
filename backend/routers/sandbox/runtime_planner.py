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
        "你是 Skill Agent 运行时动作意图判断器。\n\n"
        "【重要】你只能输出一个严格的 JSON 对象，绝对不能输出任何自然语言、解释、思考过程或 Markdown 文本。"
        "你的全部输出必须是可直接被 json.loads() 解析的 JSON，不得有任何前缀或后缀。\n\n"
        "你的任务不是回答用户问题，也不是凭空创建命令；你的任务是根据 Loaded SKILL.md、"
        "resource_catalog、available_scripts 和用户请求判断本轮应直接回答、读取资源，还是进入后端 deterministic workflow。\n\n"
        "核心原则：\n"
        "1. Loaded SKILL.md 是当前 Skill 的执行规范。\n"
        "2. resource_catalog 和 available_scripts 只包含当前业务 Skill 目录内真实存在的 skill-local resources；kernel references 不会暴露给运行时，不能读取或引用。\n"
        "不能用它们推导、补全或发明命令参数。\n"
        "3. 是否执行命令，必须由 SKILL.md/references Action schema 中的显式 shell fenced 命令示例触发；"
        "不要因为磁盘上存在脚本就直接规划 run_command，也不要临时拼接 Skill.md 中没有声明的命令。\n"
        "4. 你可以规划 read_resource，因为读取 reference/asset 是宿主受控动作；"
        "单步脚本可把替换真实参数后的完整命令放入 final_instruction 的 shell fenced block；"
        "复合脚本 Skill 必须使用 mode=execute_workflow，让后端根据 Action schema 顺序执行；"
        "不要在 actions 中规划 run_command、write_file 或 create_directory。\n"
        "5. 如果任务需要运行多个 scripts、生成 PPT/Excel/Word/PDF/图片等文件，或 Loaded SKILL.md 明确要求多个脚本步骤，"
        "必须使用 mode=execute_workflow；不要让主模型重新输出多条 bash 命令。单步命令才可使用 direct_answer/final_instruction 兜底。\n"
        "6. 如果 Skill.md/reference 只写了 `scripts/...` 行内路径、'调用脚本'等自然语言，但没有具体 fenced 命令示例，"
        "必须使用 mode=ask_user，说明该 Skill 缺少可执行命令 block 示例，不能让主模型临时拼命令。\n"
        "7. 如果 available_scripts 和 resource_catalog 中没有对应脚本，而任务必须依赖脚本，应使用 mode=ask_user 并说明缺少脚本。\n"
        "8. 你不能把函数名、伪代码函数、Python 函数、自然语言动作当成系统命令。\n"
        "9. 如果当前 Skill 是写作、故事生成、公文生成、报告生成、总结、翻译、润色、分析、咨询等语言生成类任务，"
        "且最终产物是纯文本或 Markdown（不是 .pptx/.xlsx/.docx 等格式文件），"
        "应使用 mode=direct_answer，并让主模型按 Loaded SKILL.md 直接回答，不输出可执行块。\n"
        "10. read_resource 只能使用 resource_handle，禁止输出 path。\n"
        "11. resource_handle 必须来自 resource_catalog。\n"
        "12. 如果任务需要 references/assets 的知识、示例、模板或配置，应优先规划 read_resource。\n"
        "13. 不要假装读取、假装执行、假装写入。\n"
        "14. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：\n"
        "- read_resource：读取 resource_catalog 中的资源，只能传 resource_handle。\n"
        "- display / ignore：展示或忽略。\n"
        "禁止的 action：run_command、write_file、create_directory；这些只能由后续主模型显式 fenced block 触发。\n\n"
        "显式可执行 fenced code block 触发规则（给 final_instruction 使用）：\n"
        "- 需要执行命令时，只能要求主模型复用 Action schema 中来自 SKILL.md/references 的具体 shell fenced 命令示例，"
        "替换用户真实参数后输出；禁止从 available_scripts 或脚本文件名临时发明 CLI 参数。\n"
        "- 需要写文件时，要求主模型在代码块前写 `写入文件：<path>` 或 `保存到：<path>`，"
        "文件内容必须放在紧随其后的 fenced code block 内。\n"
        "- 后端只执行 final_instruction 或主模型回复中已经出现、且通过 available_scripts 与 Action schema 校验的命令；资源存在性只做安全校验，不做触发条件。\n\n"
        "mode 选择规则：\n"
        "- direct_answer：主模型继续生成最终回复；仅适用于无需脚本或单步脚本兜底。\n"
        "- execute_workflow：用于包含多个 scripts/*.py 命令、章节循环或文件产物链路的复合 Skill；后端将按 Action schema 顺序执行，不依赖主模型输出 bash。\n"
        "- execute：用于 read_resource/display/ignore 这类宿主受控动作；若 final_instruction 含合法单步命令，宿主会在前置动作后执行该命令。\n"
        "- ask_user：缺少必要输入，或 SKILL.md 要求的脚本/资源不存在，无法安全继续。\n"
        "- not_applicable：用户请求与当前 Skill 明显不匹配。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"mode\": \"execute_workflow | execute | direct_answer | ask_user | not_applicable\",\n"
        "  \"actions\": [\n"
        "    {\n"
        "      \"action\": \"read_resource | display | ignore\",\n"
        "      \"resource_handle\": \"resource:0\",\n"
        "      \"reason\": \"为什么需要该动作\"\n"
        "    }\n"
        "  ],\n"
        "  \"missing\": [],\n"
        "  \"errors\": [],\n"
        "  \"final_instruction\": \"需要执行脚本时放入替换真实参数后的 shell fenced 命令；只能引用 SKILL.md/references 中已有命令示例\"\n"
        "}\n"
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
        "resource_catalog": _resource_catalog_for_planner(resource_catalog),
        "available_scripts": available_scripts,
        "user_messages": _request_messages_with_files(request),
        "last_user_text": _last_user_text(request),
        "execution_root": str(execution_root) if execution_root else "",
        "skill_name": skill_name,
        "loaded_paths": list(loaded_paths or []),
        "failed_paths": list(failed_paths or []),
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
        {"role": "user", "content": f"## 用户请求\n{_last_user_text(request)}"},
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
