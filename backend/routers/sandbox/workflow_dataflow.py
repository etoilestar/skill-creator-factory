"""工作流 ReAct 规划与执行。

架构：规划步骤列表 → ReAct 多轮 LLM 交互执行
- Planner 只输出步骤列表（script_path + description），不指定 input_mapping/outputs/loop
- 执行时每步由 LLM 决定调用哪个工具、传什么参数
- 工具执行结果追加到对话历史，LLM 观察后决定下一步
- 循环由 LLM 自行判断（看到上游输出后决定是否重复调用）
"""

import asyncio
import functools
import json
import logging
import re
from pathlib import Path
from typing import Any

from ...config import settings
from ...services.llm_proxy import complete_chat_once
from ...services.skill_dataflow import (
    extract_inline_context_values,
    initial_context_from_entries,
    parse_stdout_context,
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
    _workflow_payload_summary,
)
from .action_schema import _extract_script_path_from_command
from .task_executor import _execute_single_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  用户上下文构建
# ---------------------------------------------------------------------------

def _workflow_context_from_request_text(user_text: str, first_entry: dict) -> dict:
    """Build generic user-provided context without business field inference."""
    text = (user_text or "").strip()
    if not text:
        return {}
    context = {"user_request": text, "input": text, "text": text}
    context.update(extract_inline_context_values(text))
    return context


# ---------------------------------------------------------------------------
#  Planner：生成简化的步骤列表
# ---------------------------------------------------------------------------

def _workflow_step_planner_prompt() -> str:
    """Planner system prompt — 只输出步骤列表和初始上下文，不指定参数映射。"""
    return (
        "你是 workflow step planner，负责根据用户请求和可用工具规划执行步骤。\n"
        "必须只输出 JSON object，不要 Markdown 代码块。\n"
        "\n"
        "输出格式：\n"
        "{\"initial_context\":{...},\"steps\":[{\"script_path\":\"scripts/x.py\",\"description\":\"该步骤的目的\"}]}\n"
        "\n"
        "规则：\n"
        "1) initial_context：从用户请求中提取的关键变量（如 animal_name、style 等），以及 schema 默认值。用户输入覆盖默认值。\n"
        "2) steps：按执行顺序列出每个脚本步骤，script_path 必须与 Action schema entries 一致。\n"
        "3) description：简要说明该步骤的目的，帮助后续执行模型理解该步骤需要做什么。\n"
        "4) 不要输出 input_mapping、outputs、loop、collections 等字段，执行时由模型自行决定参数。\n"
        "5) 不要输出 bash 命令，不要宣称执行成功。\n"
        "6) 如果参考资料中定义了与 Action schema inputs 默认值不同的规范，initial_context 应优先使用参考资料中的值。"
    )


async def _plan_workflow_steps_with_model(
    *,
    execution_root: Path,
    action_schema: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
    model: str | None = None,
    reference_texts: dict[str, str] | None = None,
) -> dict:
    """让 LLM 生成简化的步骤列表（不含 input_mapping/outputs/loop）。"""
    entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    req = request or ChatRequest(messages=[])
    user_text = str((user_context or {}).get("user_request") or _last_user_text(req) or "")

    skill_md = ""
    skill_path = execution_root / "SKILL.md"
    if skill_path.is_file():
        skill_md = skill_path.read_text(encoding="utf-8", errors="replace")[: settings.skill_resource_max_chars]

    # 构建工具描述：每个 entry 的 script_path + inputs + command 概要
    tool_descriptions = []
    for entry in entries:
        sp = entry.get("script_path", "")
        inputs = entry.get("inputs") or []
        cmd = str(entry.get("command") or "").strip()
        # 从 command 中提取 JSON argv 的 key 列表作为参数提示
        param_hint = ""
        try:
            import shlex
            parts = shlex.split(cmd)
            for idx, part in enumerate(parts):
                if part.replace("\\", "/").lstrip("./") == sp.replace("\\", "/").lstrip("./"):
                    if idx + 1 < len(parts) and parts[idx + 1].startswith("{"):
                        try:
                            payload = json.loads(parts[idx + 1])
                            if isinstance(payload, dict):
                                param_hint = ", 参数: " + ", ".join(payload.keys())
                        except json.JSONDecodeError:
                            pass
                    break
        except ValueError:
            pass
        desc = f"- {sp}（inputs: {', '.join(inputs) if inputs else '无'}{param_hint}）"
        tool_descriptions.append(desc)

    messages = [
        {"role": "system", "content": _workflow_step_planner_prompt()},
        {"role": "user", "content": "## 用户请求\n" + user_text},
        {"role": "user", "content": "## 已知用户上下文\n" + json.dumps(user_context or {}, ensure_ascii=False)},
        {"role": "user", "content": "## SKILL.md\n" + skill_md},
        {"role": "user", "content": "## 可用工具\n" + "\n".join(tool_descriptions)},
        {"role": "user", "content": "## Action schema\n" + json.dumps(action_schema, ensure_ascii=False)},
    ]
    if reference_texts:
        ref_content = "\n\n".join(
            f"### {path}\n{text}" for path, text in reference_texts.items()
        )
        messages.append({"role": "user", "content": "## 参考资料内容\n" + ref_content})
    messages.append({"role": "user", "content": "请只输出步骤规划 JSON。"})

    planner_model = _planner_model_name(model or getattr(req, "model", None))
    try:
        planner_text = await complete_chat_once(messages, planner_model)
        raw_plan = json.loads(_strip_markdown_json_fence(planner_text))
    except Exception as exc:
        logger.warning("workflow step planner failed, building fallback: %s", exc)
        # Fallback：从 Action schema 直接构建简单步骤列表
        raw_plan = _build_fallback_step_plan(entries, user_context)

    # 基本校验
    return _validate_step_plan(raw_plan, entries)


def _build_fallback_step_plan(entries: list[dict], user_context: dict) -> dict:
    """当 LLM planner 不可用时，从 Action schema 构建简单步骤列表。"""
    steps = []
    for entry in entries:
        sp = str(entry.get("script_path") or "")
        if not sp.startswith("scripts/"):
            continue
        steps.append({
            "script_path": sp,
            "description": f"执行 {sp}",
        })
    return {
        "initial_context": dict(user_context),
        "steps": steps,
    }


def _validate_step_plan(plan: dict, entries: list[dict]) -> dict:
    """基本校验：确保 plan 有 steps 且 script_path 与 entries 对应。"""
    if not isinstance(plan, dict):
        raise ValueError("workflow step plan 必须是 JSON object")

    initial_context = plan.get("initial_context") or {}
    if not isinstance(initial_context, dict):
        initial_context = {}

    raw_steps = plan.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("workflow step plan 必须包含非空 steps 列表")

    # 校验每个 step 的 script_path
    entry_paths = [str(e.get("script_path") or "") for e in entries if str(e.get("script_path") or "").startswith("scripts/")]
    validated_steps = []
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        sp = str(step.get("script_path") or "")
        if not sp:
            continue
        # 如果 script_path 不在 entries 中但格式正确，仍保留（宽松）
        validated_steps.append({
            "script_path": sp,
            "description": str(step.get("description") or f"执行 {sp}"),
        })

    if not validated_steps:
        # 如果 LLM 没有输出有效 steps，用 entries 兜底
        validated_steps = [
            {"script_path": sp, "description": f"执行 {sp}"}
            for sp in entry_paths
        ]

    return {
        "initial_context": initial_context,
        "steps": validated_steps,
    }


# ---------------------------------------------------------------------------
#  ReAct 执行：多轮 LLM 交互
# ---------------------------------------------------------------------------

def _react_system_prompt(action_schema: dict) -> str:
    """构建 ReAct 执行的系统提示词。"""
    entries = [e for e in (action_schema.get("entries") or []) if isinstance(e, dict)]

    tool_lines = []
    for entry in entries:
        sp = str(entry.get("script_path") or "")
        if not sp.startswith("scripts/"):
            continue
        inputs = entry.get("inputs") or []
        cmd = str(entry.get("command") or "").strip()
        outputs = entry.get("outputs") or []

        # 从 command 中提取 JSON argv 的 key 列表
        param_keys = list(inputs)
        try:
            import shlex
            parts = shlex.split(cmd)
            for idx, part in enumerate(parts):
                if part.replace("\\", "/").lstrip("./") == sp.replace("\\", "/").lstrip("./"):
                    if idx + 1 < len(parts) and parts[idx + 1].startswith("{"):
                        try:
                            payload = json.loads(parts[idx + 1])
                            if isinstance(payload, dict):
                                for k in payload.keys():
                                    if k not in param_keys:
                                        param_keys.append(k)
                        except json.JSONDecodeError:
                            pass
                    break
        except ValueError:
            pass

        param_desc = f"（参数: {', '.join(param_keys)}）" if param_keys else ""
        output_desc = f" → 输出: {', '.join(str(o) for o in outputs)}" if outputs else ""
        tool_lines.append(f"  - {sp}{param_desc}{output_desc}")

    tools_str = "\n".join(tool_lines) if tool_lines else "  （无可用工具）"

    return (
        "你是一个工具调用助手。根据当前步骤的要求和已有上下文，决定需要调用哪个工具以及传递什么参数。\n"
        "\n"
        "可用工具：\n"
        + tools_str +
        "\n\n"
        "回复格式：\n"
        "- 需要调用工具时，输出 JSON：\n"
        '  {"tool_call": {"script": "scripts/xxx.py", "params": {"参数名": "参数值"}}}\n'
        "- 不需要工具时，输出 JSON：\n"
        '  {"direct_response": "直接回答内容"}\n'
        "\n"
        "规则：\n"
        "1. 根据当前步骤描述和已有上下文，选择合适的工具并构造参数。\n"
        "2. 参数值应从上下文中获取（如前序步骤的输出、用户输入等）。\n"
        "3. 如果当前步骤需要为多个项目分别调用工具（如为每个章节生成图片），请先输出一次工具调用，"
        "执行结果返回后你会看到，然后继续为下一个项目调用，直到所有项目处理完毕。\n"
        "4. 所有项目处理完毕后，输出 {\"step_complete\": true} 表示当前步骤结束。\n"
        "5. 只输出 JSON，不要输出其他内容。"
    )


def _parse_tool_call_response(response: str) -> dict:
    """解析 LLM 的工具调用响应。"""
    response = response.strip()

    # 尝试提取 JSON
    json_str = response
    # 去掉 markdown 代码块
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", response, re.DOTALL)
    if fence_match:
        json_str = fence_match.group(1).strip()

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        # 尝试找到第一个 { 和最后一个 }
        start = response.find("{")
        end = response.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(response[start:end + 1])
            except json.JSONDecodeError:
                return {"need_tool": False, "response": response}
        else:
            return {"need_tool": False, "response": response}

    if not isinstance(parsed, dict):
        return {"need_tool": False, "response": response}

    # 检查 step_complete
    if parsed.get("step_complete"):
        return {"need_tool": False, "step_complete": True}

    # 检查 tool_call
    tool_call = parsed.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("script"):
        return {
            "need_tool": True,
            "script": str(tool_call["script"]),
            "params": tool_call.get("params") or {},
        }

    # 检查 direct_response
    if "direct_response" in parsed:
        return {"need_tool": False, "response": str(parsed["direct_response"])}

    # 无法识别的格式，当作直接回复
    return {"need_tool": False, "response": response}


def _build_command_from_tool_call(script_path: str, params: dict, action_schema: dict) -> str:
    """根据工具调用信息构建可执行命令。

    优先使用 Action schema 中的 command 模板，将 params 填入 JSON argv。
    如果没有 command 模板，则直接用 python script_path + JSON params 构建。
    """
    entries = [e for e in (action_schema.get("entries") or []) if isinstance(e, dict)]

    # 查找匹配的 entry
    matched_entry = None
    for entry in entries:
        sp = str(entry.get("script_path") or "")
        if sp == script_path or sp.endswith("/" + script_path):
            matched_entry = entry
            break

    if matched_entry:
        cmd_template = str(matched_entry.get("command") or "").strip()
        if cmd_template:
            # 使用 command 模板，将 params 替换 JSON argv 中的占位符
            return _render_command_with_params(cmd_template, script_path, params)

    # Fallback：直接构建 python scripts/xxx.py '{"key": "value"}'
    params_json = json.dumps(params, ensure_ascii=False)
    import shlex
    return f"python {script_path} {shlex.quote(params_json)}"


def _render_command_with_params(command_template: str, script_path: str, params: dict) -> str:
    """将 params 填入 command 模板的 JSON argv 中。"""
    import shlex
    try:
        parts = shlex.split(command_template)
    except ValueError:
        # 模板解析失败，fallback
        params_json = json.dumps(params, ensure_ascii=False)
        return f"python {script_path} {shlex.quote(params_json)}"

    # 找到 JSON argv 的位置
    json_idx = None
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized == script_path.replace("\\", "/").lstrip("./") or normalized.endswith("/" + script_path.replace("\\", "/").lstrip("./")):
            json_idx = idx + 1 if idx + 1 < len(parts) else None
            break

    if json_idx is not None and parts[json_idx].startswith("{"):
        try:
            template_payload = json.loads(parts[json_idx])
            if isinstance(template_payload, dict):
                # 用 params 覆盖模板中的占位符值
                merged = dict(template_payload)
                for key, value in params.items():
                    merged[key] = value
                # 替换剩余的 {{placeholder}} 为 params 中的值
                for key in list(merged.keys()):
                    val = merged[key]
                    if isinstance(val, str) and re.match(r"^\{\{.+\}\}$", val):
                        placeholder_key = val.strip("{}").strip()
                        if placeholder_key in params:
                            merged[key] = params[placeholder_key]
                parts[json_idx] = json.dumps(merged, ensure_ascii=False)
                return " ".join(shlex.quote(part) for part in parts)
        except (json.JSONDecodeError, IndexError):
            pass

    # 无法替换 JSON argv，直接用 params 构建
    params_json = json.dumps(params, ensure_ascii=False)
    return f"python {script_path} {shlex.quote(params_json)}"


# 单个步骤内最大工具调用次数（防止无限循环）
_MAX_TOOL_CALLS_PER_STEP = 20


async def _execute_workflow_with_react_loop(
    *,
    execution_root: Path,
    action_schema: dict,
    step_plan: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
    model: str | None = None,
    yield_func=None,
) -> dict:
    """ReAct 多轮 LLM 交互执行工作流。

    对每个步骤：
    1. 发送步骤描述 + 上下文 + 可用工具给 LLM
    2. LLM 输出 tool_call JSON（选择工具 + 参数）
    3. 执行工具，将结果追加到对话历史
    4. LLM 观察结果，决定是否继续调用或完成步骤

    Args:
        yield_func: async callable，用于向前端发送 SSE 事件（接收 str 或 None）
    """
    entries = [e for e in (action_schema.get("entries") or []) if isinstance(e, dict)]
    entries = [e for e in entries if str(e.get("script_path") or "").startswith("scripts/")]
    if not entries:
        raise ValueError("execute_workflow requires at least one scripts/* entry")

    root = execution_root.resolve()
    req = request or ChatRequest(messages=[])
    user_text = str((user_context or {}).get("user_request") or _last_user_text(req) or "")
    session_input_dir = _extract_input_session_dir(getattr(req, "input_files", []) or [], root)

    # 构建初始上下文
    initial_context = step_plan.get("initial_context") or {}
    context: dict[str, Any] = {**user_context, **initial_context}

    # 构建对话历史
    conversation: list[dict[str, str]] = [
        {"role": "system", "content": _react_system_prompt(action_schema)},
        {"role": "user", "content": f"用户请求：{user_text}"},
    ]

    results: list[dict] = []
    output_files: list[dict] = []
    touched: list[Path] = []
    workflow_logs: list[str] = []

    steps = step_plan.get("steps") or []

    # 输出执行计划到前端
    plan_summary = "执行计划：\n"
    for i, step in enumerate(steps):
        plan_summary += f"  {i + 1}. {step.get('script_path', '?')} — {step.get('description', '')}\n"
    plan_summary += "\n开始逐步执行..."

    if yield_func:
        await yield_func(_sse_react_event("plan", plan_summary, {"steps": steps}))

    for step_index, step in enumerate(steps):
        script_path = str(step.get("script_path") or "")
        description = str(step.get("description") or f"执行 {script_path}")

        # 向对话历史添加当前步骤提示
        step_prompt = (
            f"\n---\n当前步骤 [{step_index + 1}/{len(steps)}]：{description}\n"
            f"脚本路径：{script_path}\n"
            f"当前上下文：{json.dumps(_safe_context_for_prompt(context), ensure_ascii=False)}\n"
            f"请决定如何调用工具完成此步骤。"
        )
        conversation.append({"role": "user", "content": step_prompt})

        if yield_func:
            await yield_func(_sse_react_event(
                "step_start",
                f"步骤 [{step_index + 1}/{len(steps)}]：{description}",
                {"step_index": step_index, "script_path": script_path, "description": description},
            ))

        # ReAct 循环：在当前步骤内可能多次调用工具
        tool_call_count = 0
        step_complete = False

        while not step_complete and tool_call_count < _MAX_TOOL_CALLS_PER_STEP:
            tool_call_count += 1
            react_model = _planner_model_name(model or getattr(req, "model", None))

            try:
                llm_response = await complete_chat_once(conversation, react_model)
            except Exception as exc:
                logger.error("ReAct LLM call failed at step %d call %d: %s", step_index, tool_call_count, exc)
                workflow_logs.append(f"步骤 {step_index} 第 {tool_call_count} 次 LLM 调用失败: {exc}")
                break

            conversation.append({"role": "assistant", "content": llm_response})
            parsed = _parse_tool_call_response(llm_response)

            if parsed.get("step_complete"):
                # LLM 声明当前步骤完成
                step_complete = True
                if yield_func:
                    await yield_func(_sse_react_event(
                        "step_complete",
                        f"步骤 [{step_index + 1}/{len(steps)}] 完成（共 {tool_call_count} 次工具调用）",
                        {"step_index": step_index, "tool_call_count": tool_call_count},
                    ))
                workflow_logs.append(
                    f"步骤 {step_index} ({script_path}) 完成，共 {tool_call_count} 次工具调用"
                )
                break

            if not parsed.get("need_tool"):
                # LLM 直接回复，不调用工具
                direct_response = parsed.get("response", "")
                if yield_func:
                    await yield_func(_sse_react_event(
                        "llm_response",
                        direct_response[:500],
                        {"step_index": step_index},
                    ))
                # 如果是直接回复，视为步骤完成
                step_complete = True
                workflow_logs.append(f"步骤 {step_index} LLM 直接回复，无需工具调用")
                break

            # 需要调用工具
            tool_script = parsed["script"]
            tool_params = parsed["params"]

            if yield_func:
                await yield_func(_sse_react_event(
                    "tool_call",
                    f"调用工具：{tool_script}",
                    {"step_index": step_index, "script": tool_script, "params": tool_params},
                ))

            # 构建并执行命令
            command = _build_command_from_tool_call(tool_script, tool_params, action_schema)

            logger.info(
                "ReAct step[%d] call[%d] script=%s params=%s command=%s",
                step_index, tool_call_count, tool_script,
                json.dumps(tool_params, ensure_ascii=False)[:200],
                command[:300],
            )

            result, task_touched = await asyncio.to_thread(
                functools.partial(
                    _execute_single_task,
                    {"action": "run_command", "command": command, "reason": f"ReAct step {step_index} tool call"},
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

            # 构建工具执行结果
            raw_stdout = str(result.get("stdout") or "")
            raw_stderr = str(result.get("stderr") or "")
            success = result.get("success", True)
            returncode = result.get("returncode", 0)

            # 解析 stdout JSON
            stdout_payload = {}
            if raw_stdout.strip():
                try:
                    stdout_payload = parse_stdout_context(raw_stdout)
                except ValueError:
                    stdout_payload = {"raw_output": raw_stdout[:2000]}

            # 更新 context
            if stdout_payload:
                # 扁平合并到 context
                context.update(stdout_payload)
                # 以脚本名为 namespace 存储
                script_stem = Path(tool_script).stem
                context[script_stem] = stdout_payload

            # 将工具执行结果追加到对话历史
            tool_result_msg = (
                f"[工具执行结果] {tool_script}\n"
                f"执行状态：{'成功' if success else '失败'} (returncode={returncode})\n"
            )
            if stdout_payload:
                # 截断过长的输出，保留关键信息
                payload_str = json.dumps(stdout_payload, ensure_ascii=False)
                if len(payload_str) > 3000:
                    payload_str = payload_str[:3000] + "...（已截断）"
                tool_result_msg += f"输出：{payload_str}\n"
            if raw_stderr.strip():
                tool_result_msg += f"错误输出：{raw_stderr[:500]}\n"
            if not success:
                tool_result_msg += "请检查错误信息，可以尝试调整参数重新调用。\n"

            conversation.append({"role": "user", "content": tool_result_msg})

            if yield_func:
                # 输出工具执行结果到前端
                result_summary = _summarize_tool_result(tool_script, stdout_payload, success)
                await yield_func(_sse_react_event(
                    "tool_result",
                    result_summary,
                    {
                        "step_index": step_index,
                        "script": tool_script,
                        "success": success,
                        "returncode": returncode,
                        "output_keys": list(stdout_payload.keys()) if isinstance(stdout_payload, dict) else [],
                    },
                ))

            workflow_logs.append(
                f"步骤 {step_index} 调用 {tool_script} "
                f"({'成功' if success else '失败'} returncode={returncode})"
            )

        # 步骤结束
        if not step_complete:
            logger.warning("ReAct step %d reached max tool calls (%d)", step_index, tool_call_count)
            workflow_logs.append(
                f"步骤 {step_index} 达到最大工具调用次数 {_MAX_TOOL_CALLS_PER_STEP}"
            )

    # 所有步骤完成
    if yield_func:
        await yield_func(_sse_react_event(
            "workflow_complete",
            f"工作流执行完成：{len(steps)} 个步骤，{len(results)} 次工具调用",
            {
                "step_count": len(steps),
                "tool_call_count": len(results),
                "output_file_count": len(output_files),
                "context_keys": sorted(context.keys()),
            },
        ))
        # 发送结束信号，让队列读取循环退出
        await yield_func(None)

    return {
        "executed": True,
        "results": results,
        "context": context,
        "output_files": output_files,
        "touched_paths": [str(p) for p in touched],
        "logs": workflow_logs,
    }


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------

def _sse_react_event(event_type: str, message: str, data: dict | None = None) -> str:
    """构建 ReAct 执行过程的 SSE 事件，以 thought 形式输出到前端对话流。"""
    from ..chat_utils import _sse, _thought
    return _thought(
        f"react_{event_type}",
        message[:80] if len(message) > 80 else message,
        message,
        data or {},
    )


def _safe_context_for_prompt(context: dict) -> dict:
    """将 context 转为 LLM prompt 安全的格式（截断过长的值）。"""
    safe = {}
    for key, value in context.items():
        if isinstance(value, str):
            safe[key] = value[:500] if len(value) > 500 else value
        elif isinstance(value, (dict, list)):
            s = json.dumps(value, ensure_ascii=False)
            safe[key] = s[:1000] + "...（已截断）" if len(s) > 1000 else value
        else:
            safe[key] = value
    return safe


def _summarize_tool_result(script: str, payload: dict, success: bool) -> str:
    """生成工具执行结果的简短摘要。"""
    if not success:
        return f"❌ {script} 执行失败"
    if not payload:
        return f"✓ {script} 执行成功（无结构化输出）"
    keys = list(payload.keys())
    summary_parts = [f"✓ {script} 执行成功"]
    for key in keys[:5]:  # 最多展示 5 个字段
        val = payload[key]
        if isinstance(val, str):
            summary_parts.append(f"  {key}: {val[:100]}")
        elif isinstance(val, (dict, list)):
            s = json.dumps(val, ensure_ascii=False)
            summary_parts.append(f"  {key}: {s[:150]}{'...' if len(s) > 150 else ''}")
        else:
            summary_parts.append(f"  {key}: {val}")
    if len(keys) > 5:
        summary_parts.append(f"  ... 共 {len(keys)} 个字段")
    return "\n".join(summary_parts)


# ---------------------------------------------------------------------------
#  主入口
# ---------------------------------------------------------------------------

async def _execute_skill_workflow(
    *,
    execution_root: Path,
    action_schema: dict,
    user_context: dict,
    request: ChatRequest | None = None,
    skill_name: str = "",
    dataflow_plan: dict | None = None,
    model: str | None = None,
    yield_func=None,
) -> dict:
    """规划步骤列表，然后通过 ReAct 多轮 LLM 交互执行。"""
    # 1. 规划步骤
    if dataflow_plan is None:
        from .action_schema import _reference_contract_texts
        reference_texts = _reference_contract_texts(execution_root)
        try:
            dataflow_plan = await _plan_workflow_steps_with_model(
                execution_root=execution_root,
                action_schema=action_schema,
                user_context=user_context,
                request=request,
                skill_name=skill_name,
                model=model,
                reference_texts=reference_texts,
            )
        except Exception as exc:
            logger.warning("workflow step planner failed, using fallback: %s", exc)
            entries = [e for e in (action_schema.get("entries") or []) if isinstance(e, dict)]
            dataflow_plan = _build_fallback_step_plan(entries, user_context)

    # 2. ReAct 执行
    return await _execute_workflow_with_react_loop(
        execution_root=execution_root,
        action_schema=action_schema,
        step_plan=dataflow_plan,
        user_context=user_context,
        request=request,
        skill_name=skill_name,
        model=model,
        yield_func=yield_func,
    )


# Public aliases
execute_skill_workflow = _execute_skill_workflow

# Backward-compatible re-exports (used by sandbox_chat.py and tests)
from ...services.skill_dataflow import (  # noqa: E402, F401
    validate_workflow_dataflow_plan,
    merge_step_output,
)


def render_command_template(command: str, context: dict) -> str:  # noqa: F401
    """Backward-compatible alias — 渲染命令模板中的占位符。"""
    import shlex
    from .action_schema import _RUNTIME_PLACEHOLDER_RE
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"dataflow_mismatch: 命令模板无法解析: {command}") from exc
    script_path = _extract_script_path_from_command(command) or ""
    json_idx = None
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized == script_path.replace("\\", "/").lstrip("./") or normalized.endswith("/" + script_path.replace("\\", "/").lstrip("./")):
            json_idx = idx + 1 if idx + 1 < len(parts) else None
            break
    if json_idx is not None and parts[json_idx].startswith("{"):
        try:
            payload = json.loads(parts[json_idx])
            if isinstance(payload, dict):
                from ...services.skill_dataflow import replace_placeholders_in_value, missing_placeholders, extract_placeholders
                missing = missing_placeholders(extract_placeholders(payload), context)
                if missing:
                    needed = ", ".join(f"{{{{{key}}}}}" for key in missing)
                    raise ValueError(f"dataflow_mismatch: 缺少变量 {needed}")
                parts[json_idx] = json.dumps(replace_placeholders_in_value(payload, context), ensure_ascii=False)
                return " ".join(shlex.quote(part) for part in parts)
        except (json.JSONDecodeError, IndexError):
            pass

    from ...services.skill_dataflow import resolve_context_value
    missing = missing_placeholders(set(_RUNTIME_PLACEHOLDER_RE.findall(command)), context)
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
