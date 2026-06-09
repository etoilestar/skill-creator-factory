"""工作流数据流规划与执行。"""

import asyncio
import functools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

from ...config import settings
from ...services.llm_proxy import complete_chat_once
from ...services.skill_dataflow import (
    DataflowError,
    LoopExpansionError,
    MissingVariablesError,
    apply_dataflow_collections,
    collect_loop_outputs,
    context_from_dataflow_plan,
    deterministic_dataflow_plan_from_schema,
    expand_step_contexts,
    extract_inline_context_values,
    extract_placeholders,
    initial_context_from_entries,
    merge_step_output as merge_dataflow_step_output,
    missing_placeholders,
    parse_stdout_context,
    placeholder_pattern,
    replace_placeholders_in_value,
    resolve_context_value,
    materialize_step_contexts_from_plan,
    validate_and_align_step_stdout,
    validate_workflow_dataflow_plan as _validate_workflow_dataflow_plan_impl,
    workflow_loop_collection_path,
)
from ..chat_utils import (
    _extract_input_session_dir,
    _last_user_text,
    _planner_model_name,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest
from .path_resolution import (
    _normalize_skill_resource_path,
    _available_scripts_for_root,
    _workflow_value_preview,
    _workflow_payload_summary,
)
from .action_schema import _extract_script_path_from_command
from .task_executor import _execute_single_task

logger = logging.getLogger(__name__)


def _workflow_context_from_request_text(user_text: str, first_entry: dict) -> dict:
    """Build generic user-provided context without business field inference."""
    text = (user_text or "").strip()
    if not text:
        return {}
    context = {"user_request": text, "input": text, "text": text}
    context.update(extract_inline_context_values(text))
    return context


def _missing_workflow_placeholders(entry: dict, context: dict) -> list[str]:
    return missing_placeholders(entry.get("placeholder_keys") or [], context)


def _json_arg_index(parts: list[str], script_path: str) -> int | None:
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized == script_path or normalized.endswith("/" + script_path):
            return idx + 1 if idx + 1 < len(parts) else None
    return None


def _placeholder_keys_in_value(value: object) -> set[str]:
    return extract_placeholders(value)


def _missing_placeholder_keys(keys: set[str], context: dict) -> list[str]:
    return missing_placeholders(keys, context)


def _replace_placeholders_in_value(value: object, context: dict) -> object:
    return replace_placeholders_in_value(value, context)



def render_command_template(command: str, context: dict) -> str:
    """Render Action schema command placeholders without asking the LLM to re-emit bash."""
    from .action_schema import _RUNTIME_PLACEHOLDER_RE
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"dataflow_mismatch: 命令模板无法解析: {command}") from exc
    script_path = _extract_script_path_from_command(command) or ""
    json_idx = _json_arg_index(parts, script_path) if script_path else None
    if json_idx is not None:
        json_candidate = parts[json_idx].strip()
        if json_candidate.startswith("{"):
            try:
                payload = json.loads(json_candidate)
            except json.JSONDecodeError as exc:
                raise ValueError(f"dataflow_mismatch: {script_path} 的 JSON argv 无法解析") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"dataflow_mismatch: {script_path} 的 JSON argv 必须是 object")
            missing = _missing_placeholder_keys(_placeholder_keys_in_value(payload), context)
            if missing:
                needed = ", ".join(f"{{{{{key}}}}}" for key in missing)
                raise ValueError(f"dataflow_mismatch: 缺少变量 {needed}")
            parts[json_idx] = json.dumps(_replace_placeholders_in_value(payload, context), ensure_ascii=False)
            return " ".join(shlex.quote(part) for part in parts)

    missing = _missing_placeholder_keys(set(_RUNTIME_PLACEHOLDER_RE.findall(command)), context)
    if missing:
        needed = ", ".join(f"{{{{{key}}}}}" for key in missing)
        raise ValueError(f"dataflow_mismatch: 缺少变量 {needed}")

    def repl(match: re.Match) -> str:
        key = match.group(1)
        try:
            value = resolve_context_value(context, key)
        except KeyError as exc:
            raise ValueError(f"dataflow_mismatch: 缺少变量 {{{{{key}}}}}") from exc
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return _RUNTIME_PLACEHOLDER_RE.sub(repl, command)


def _parse_stdout_json(stdout: str) -> dict:
    try:
        payload = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def merge_step_output(context: dict, script_path: str, stdout_json: dict) -> dict:
    """Merge one script stdout JSON into workflow context."""
    return merge_dataflow_step_output(context, script_path, stdout_json)



def _workflow_step_contexts(entry: dict, context: dict) -> list[dict]:
    return expand_step_contexts(entry, context)



def _workflow_output_summary(results: list[dict], output_files: list[dict]) -> str:
    successful = [r for r in results if r.get("success", True)]
    lines = [f"workflow 执行成功：{len(successful)} 个步骤"]
    if output_files:
        lines.append("产物路径：" + ", ".join(item.get("path", "") for item in output_files if item.get("path")))
    return "\n".join(lines)


def _workflow_dataflow_planner_prompt() -> str:
    return (
        "你是 workflow dataflow planner，只负责在执行前梳理变量流转，不执行脚本。\n"
        "输入包括用户请求、SKILL.md、Action schema、可用脚本。\n"
        "必须只输出 JSON object，不要 Markdown。\n"
        "输出格式：{\"initial_context\":{},\"steps\":[{\"script_path\":\"scripts/x.py\",\"input_mapping\":{},\"loop\":null,\"outputs\":[],\"output_policy\":\"merge_stdout\"}],\"collections\":[],\"missing\":[],\"errors\":[]}。\n"
        "规则：1) initial_context 合并用户明确输入与 schema 默认值；用户输入覆盖默认值。\n"
        "2) steps 必须与 Action schema entries 顺序和 script_path 完全一致。\n"
        "3) input_mapping 的每个 command placeholder 都必须有来源，可写成 {{变量}}、{{loop_item.field}} 或 {\"source\":\"context\",\"path\":\"变量\"}。\n"
        "4) 如果步骤遍历列表，loop 写 {\"collection\":\"上游列表变量\",\"item_name\":\"loop_item\"}；不循环为 null。\n"
        "5) 每步 outputs 填该脚本 stdout 必须提供的通用字段；可沿用 Action schema outputs。\n"
        "6) 循环输出需要聚合给后续步骤时，在 collections 声明 target/source，可选 script_path/step_index 限定来源；不要伪造脚本输出。\n"
        "7) 无法从用户、默认值、前序 stdout 或循环 item 解决时，填 missing/errors，后端会拒绝执行。\n"
        "8) 不要输出 bash，不要宣称执行成功。"
    )


async def _plan_workflow_dataflow_with_model(
    *,
    execution_root: Path,
    action_schema: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
    model: str | None = None,
) -> dict:
    """Ask the planner model to build a structured dataflow plan for workflow execution."""
    entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    req = request or ChatRequest(messages=[])
    user_text = str((user_context or {}).get("user_request") or _last_user_text(req) or "")
    skill_md = ""
    skill_path = execution_root / "SKILL.md"
    if skill_path.is_file():
        skill_md = skill_path.read_text(encoding="utf-8", errors="replace")[: settings.skill_resource_max_chars]
    messages = [
        {"role": "system", "content": _workflow_dataflow_planner_prompt()},
        {"role": "user", "content": "## 用户请求\n" + user_text},
        {"role": "user", "content": "## 已知用户上下文\n" + json.dumps(user_context or {}, ensure_ascii=False)},
        {"role": "user", "content": "## SKILL.md\n" + skill_md},
        {"role": "user", "content": "## Action schema\n" + json.dumps(action_schema, ensure_ascii=False)},
        {"role": "user", "content": "## 可用脚本\n" + json.dumps(_available_scripts_for_root(execution_root), ensure_ascii=False)},
        {"role": "user", "content": "请只输出 workflow dataflow plan JSON。"},
    ]
    planner_model = _planner_model_name(model or getattr(req, "model", None))
    try:
        planner_text = await complete_chat_once(messages, planner_model)
        raw_plan = json.loads(_strip_markdown_json_fence(planner_text))
    except Exception as exc:
        logger.warning("workflow dataflow planner unavailable/invalid, using schema-derived fallback: %s", exc)
        raw_plan = deterministic_dataflow_plan_from_schema(entries, user_text=user_text, user_context=user_context or {})
    return _validate_workflow_dataflow_plan(raw_plan, entries)


def _validate_workflow_dataflow_plan(plan: dict, action_schema: dict | list[dict]) -> dict:
    if isinstance(action_schema, dict):
        entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    else:
        entries = [entry for entry in action_schema if isinstance(entry, dict)]
    return _validate_workflow_dataflow_plan_impl(plan, entries)


async def _execute_workflow_from_dataflow_plan(
    *,
    execution_root: Path,
    action_schema: dict,
    dataflow_plan: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
) -> dict:
    entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    entries = [
        entry for entry in entries
        if str(entry.get("script_path") or "").startswith("scripts/")
    ]
    if not entries:
        raise ValueError("execute_workflow requires at least one scripts/* entry")

    plan = _validate_workflow_dataflow_plan_impl(dataflow_plan, entries)
    root = execution_root.resolve()
    available_scripts = set(_available_scripts_for_root(root))
    req = request or ChatRequest(messages=[])
    user_text = str((user_context or {}).get("user_request") or _last_user_text(req) or "")
    context = context_from_dataflow_plan(plan, entries, user_text=user_text, user_context=user_context or {})
    session_input_dir = _extract_input_session_dir(getattr(req, "input_files", []) or [], root)
    results: list[dict] = []
    touched: list[Path] = []
    output_files: list[dict] = []
    workflow_logs: list[str] = []
    previous_stdout_keys: list[str] = []
    previous_stdout_summary: dict[str, Any] = {}
    step_plans = plan.get("steps") or []

    for entry_index, (entry, step_plan) in enumerate(zip(entries, step_plans, strict=False)):
        script_path = str(entry.get("script_path") or "")
        command_template = str(entry.get("command") or "").strip()
        loop_info = step_plan.get("loop") if isinstance(step_plan, dict) else None
        collection_path = workflow_loop_collection_path(loop_info)

        before_log = (
            f"workflow step[{entry_index}] BEFORE script={script_path} "
            f"context_keys={sorted(context.keys())} "
            f"previous_stdout_keys={previous_stdout_keys} "
            f"previous_stdout_summary={json.dumps(previous_stdout_summary, ensure_ascii=False)} "
            f"input_mapping={json.dumps(step_plan.get('input_mapping') or {}, ensure_ascii=False)} "
            f"loop={json.dumps(loop_info, ensure_ascii=False)} "
            f"collection_path={collection_path}"
        )
        logger.info(before_log)
        workflow_logs.append(before_log)

        try:
            step_contexts = materialize_step_contexts_from_plan(step_plan, entry, context)
        except LoopExpansionError as exc:
            try:
                collection_value = resolve_context_value(context, collection_path) if collection_path else None
                collection_error = ""
            except Exception as detail_exc:
                collection_value = None
                collection_error = str(detail_exc)

            collection_summary = _workflow_value_preview(collection_value)
            error_log = (
                f"workflow LOOP EXPANSION FAILED script={script_path} "
                f"collection_path={collection_path} "
                f"collection_error={collection_error} "
                f"collection_summary={json.dumps(collection_summary, ensure_ascii=False)} "
                f"context_keys={sorted(context.keys())} "
                f"previous_stdout_keys={previous_stdout_keys}"
            )
            logger.error(error_log)
            workflow_logs.append(error_log)
            raise ValueError(f"循环变量无法展开：{script_path} 需要 {', '.join(exc.missing)}, collection 内容无效") from exc

        step_payloads: list[dict] = []
        for step_context in step_contexts:
            command = render_command_template(command_template, step_context)
            result, task_touched = await asyncio.to_thread(
                functools.partial(
                    _execute_single_task,
                    {"action": "run_command", "command": command, "reason": "execute_workflow dataflow plan step"},
                    [],
                    req,
                    execution_root=root,
                    inferred_skill_root=root,
                    skill_name=skill_name or root.name,
                    session_input_dir=session_input_dir,
                )
            )
            results.append(result)
            touched.extend(task_touched)
            output_files.extend(result.get("output_files") or [])

            raw_stdout = str(result.get("stdout") or "")
            try:
                payload = parse_stdout_context(raw_stdout)
            except ValueError as exc:
                # Non-JSON stdout (e.g., progress messages, empty output) should
                # not crash the entire workflow.  Degrade to an empty dict and
                # log a warning so downstream steps still execute.
                logger.warning(
                    "workflow step %s stdout is not valid JSON, treating as empty context: %s  raw_stdout=%.300s",
                    script_path, exc, raw_stdout,
                )
                workflow_logs.append(
                    f"workflow step {script_path} stdout is not valid JSON, treating as empty context: {exc}"
                )
                payload = {}

            payload_before_align_summary = _workflow_payload_summary(dict(payload))

            try:
                payload = validate_and_align_step_stdout(entry, step_plan, payload)
            except DataflowError as exc:
                reconcile_log = (
                    f"workflow stdout reconcile failed script={script_path} "
                    f"expected_entry_outputs={entry.get('outputs') or []} "
                    f"expected_plan_outputs={step_plan.get('outputs') if isinstance(step_plan, dict) else []} "
                    f"stdout_keys={sorted(payload.keys())} "
                    f"stdout_summary={json.dumps(_workflow_payload_summary(payload), ensure_ascii=False)} "
                    f"context_keys={sorted(context.keys())} "
                    f"error={exc}"
                )
                logger.error(reconcile_log)
                workflow_logs.append(reconcile_log)
                raise ValueError(f"workflow_stdout_mismatch: {script_path} stdout 与 plan/schema outputs 不一致") from exc

            step_payloads.append(payload)
            merge_step_output(context, script_path, payload)

            stdout_summary = _workflow_payload_summary(payload)
            after_log = (
                f"workflow step[{entry_index}] AFTER script={script_path} "
                f"stdout_raw={raw_stdout[:1000]} "
                f"stdout_json_keys={sorted(payload.keys())} "
                f"stdout_json_summary={json.dumps(stdout_summary, ensure_ascii=False)} "
                f"stdout_before_align_summary={json.dumps(payload_before_align_summary, ensure_ascii=False)} "
                f"context_keys={sorted(context.keys())}"
            )
            logger.info(after_log)
            workflow_logs.append(after_log)
            previous_stdout_keys = sorted(payload.keys())
            previous_stdout_summary = stdout_summary

        # 集合聚合
        collection_updates = apply_dataflow_collections(
            plan.get("collections") or [],
            context,
            step_payloads,
            script_path=script_path,
            step_index=entry_index,
        )
        if collection_updates:
            collection_updates_summary = _workflow_payload_summary(collection_updates)
            collection_log = (
                f"workflow step[{entry_index}] plan_collections "
                f"keys={sorted(collection_updates.keys())} "
                f"summary={json.dumps(collection_updates_summary, ensure_ascii=False)} "
                f"context_keys={sorted(context.keys())}"
            )
            logger.info(collection_log)
            workflow_logs.append(collection_log)

    return {
        "executed": True,
        "results": results,
        "context": context,
        "output_files": output_files,
        "touched_paths": [str(p) for p in touched],
        "logs": workflow_logs,
    }


async def _execute_skill_workflow(
    *,
    execution_root: Path,
    action_schema: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
    dataflow_plan: dict | None = None,
) -> dict:
    """Plan workflow dataflow first, then execute scripts deterministically."""
    if dataflow_plan is None:
        dataflow_plan = await _plan_workflow_dataflow_with_model(
            execution_root=execution_root,
            action_schema=action_schema,
            user_context=user_context,
            request=request,
            skill_name=skill_name,
        )
    return await _execute_workflow_from_dataflow_plan(
        execution_root=execution_root,
        action_schema=action_schema,
        dataflow_plan=dataflow_plan,
        user_context=user_context,
        request=request,
        skill_name=skill_name,
    )


async def _execute_skill_workflow_legacy(
    *,
    execution_root: Path,
    action_schema: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
) -> dict:
    """Execute declared Action schema script entries in order with stdout JSON dataflow."""
    entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    entries = [
        entry for entry in entries
        if _normalize_skill_resource_path(str(entry.get("script_path") or "")).startswith("scripts/")
    ]
    if not entries:
        raise ValueError("execute_workflow 需要至少一个 scripts/* Action schema entry")

    root = execution_root.resolve()
    available_scripts = set(_available_scripts_for_root(root))
    req = request or ChatRequest(messages=[])
    user_text = str((user_context or {}).get("user_request") or _last_user_text(req) or "")
    context = initial_context_from_entries(entries, user_text=user_text, user_context=user_context or {})
    session_input_dir = _extract_input_session_dir(getattr(req, "input_files", []) or [], root)
    results: list[dict] = []
    touched: list[Path] = []
    output_files: list[dict] = []
    for entry_index, entry in enumerate(entries):
        script_path = _normalize_skill_resource_path(str(entry.get("script_path") or ""))
        if script_path not in available_scripts:
            raise ValueError(f"workflow_mismatch: {script_path} 不在 available_scripts 中：{sorted(available_scripts)}")
        command_template = str(entry.get("command") or "").strip()
        if not command_template:
            raise ValueError(f"workflow_mismatch: {script_path} 缺少 command template")
        if entry_index == 0:
            missing_initial = _missing_workflow_placeholders(entry, context)
            if missing_initial:
                needed = ", ".join(f"{{{{{key}}}}}" for key in missing_initial)
                raise ValueError(f"初始输入解析失败：{script_path} 需要 {needed}，但用户输入中没有解析出对应变量。")
        try:
            step_contexts = _workflow_step_contexts(entry, context)
        except LoopExpansionError as exc:
            needed = ", ".join(f"{{{{{key}}}}}" for key in exc.missing)
            raise ValueError(f"循环变量无法展开：{script_path} 需要 {needed}，但 context 中没有可展开的列表变量。") from exc
        except MissingVariablesError as exc:
            needed = ", ".join(f"{{{{{key}}}}}" for key in exc.missing)
            if entry_index == 0:
                raise ValueError(f"初始输入解析失败：{script_path} 需要 {needed}，但用户输入或 SKILL.md 默认值中没有对应变量。") from exc
            raise ValueError(f"数据流未打通：{script_path} 需要 {needed}，但前序步骤没有产生对应变量。") from exc
        step_payloads: list[dict] = []
        for step_context in step_contexts:
            try:
                command = render_command_template(command_template, step_context)
            except ValueError as exc:
                missing = getattr(exc, "missing", None) or (entry.get("placeholder_keys") or [])
                needed = ", ".join(f"{{{{{key}}}}}" for key in missing)
                if entry_index == 0:
                    raise ValueError(f"初始输入解析失败：{script_path} 需要 {needed}，但用户输入或 SKILL.md 默认值中没有对应变量。{exc}") from exc
                raise ValueError(f"数据流未打通：{script_path} 需要 {needed}，但前序步骤没有产生对应变量。{exc}") from exc
            result, task_touched = await asyncio.to_thread(
                functools.partial(
                    _execute_single_task,
                    {"action": "run_command", "command": command, "reason": "execute_workflow Action schema step"},
                    [],
                    req,
                    execution_root=root,
                    inferred_skill_root=root,
                    skill_name=skill_name or root.name,
                    session_input_dir=session_input_dir,
                )
            )
            results.append(result)
            touched.extend(task_touched)
            output_files.extend(result.get("output_files") or [])
            if not result.get("success", True):
                raise ValueError(
                    f"workflow_step_failed: {script_path} returncode={result.get('returncode')} stderr={(result.get('stderr') or '').strip()}"
                )
            try:
                payload = parse_stdout_context(str(result.get("stdout") or ""))
            except ValueError as exc:
                logger.warning(
                    "workflow legacy step %s stdout is not valid JSON, treating as empty context: %s",
                    script_path, exc,
                )
                payload = {}
            step_payloads.append(payload)
            merge_step_output(context, script_path, payload)
        if len(step_contexts) > 1:
            merge_step_output(context, script_path, collect_loop_outputs(step_payloads, entry))

    dedup_output_files: list[dict] = []
    seen_outputs: set[str] = set()
    for item in output_files:
        path = str(item.get("path") or "")
        if not path or path in seen_outputs:
            continue
        seen_outputs.add(path)
        dedup_output_files.append(item)

    return {
        "executed": True,
        "reason": "已根据 Action schema 确定性执行 workflow。",
        "results": results,
        "context": context,
        "output_files": dedup_output_files,
        "touched_paths": [str(path) for path in touched],
        "logs": [_workflow_output_summary(results, dedup_output_files)],
    }


# Public aliases
execute_skill_workflow = _execute_skill_workflow
validate_workflow_dataflow_plan = _validate_workflow_dataflow_plan
