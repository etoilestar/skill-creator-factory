"""Creator-mode chat helpers and routes."""

import asyncio
import functools
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import settings
from ..services.kernel_loader import load_kernel_creator_body_prompt, read_skill_resource_text
from ..services.llm_proxy import complete_chat_once, stream_chat
from .chat_models import ChatRequest
from .chat_utils import (
    _last_user_text,
    _allowed_skill_roots,
    _extract_input_session_dir,
    _find_created_skill_roots,
    _friendly_error,
    _is_within_sandbox,
    _planner_model_name,
    _request_messages_with_files,
    _sse,
    _strip_markdown_json_fence,
    _thought,
    _validate_skill_md,
)
from .sandbox_chat import (
    _execute_single_task,
    _format_execution_report,
    _infer_skill_root_from_tasks,
    _plan_and_execute_generated_output,
    _run_skill_runtime_planner_round,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

_CREATOR_PHASE3_MARKER = '{"creator_phase":"phase3_start"}'


def _extract_creator_resource_catalog(body_prompt: str) -> list[dict]:
    """Extract creator resources from prompt references without sandbox coupling."""
    catalog: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"`(?P<path>(references|assets|scripts)/[^`]+)`(?P<title>：[^\n]+)?",
        re.M,
    )

    for match in pattern.finditer(body_prompt):
        path = match.group("path").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        kind = path.split("/", 1)[0]
        title = (match.group("title") or "").lstrip("：").strip()
        usage_hint = {
            "references": "设计/规范参考资料，按需阅读。",
            "assets": "模板或配置样例，按需阅读。",
            "scripts": "实现参考脚本，可读取其内容辅助生成。",
        }.get(kind, "按需阅读。")
        catalog.append(
            {
                "resource_handle": f"resource:{len(catalog)}",
                "kind": kind,
                "path": path,
                "title": title,
                "usage_hint": usage_hint,
            }
        )

    return catalog


def _creator_resource_catalog_for_selector(catalog: list[dict]) -> list[dict]:
    return [
        {
            "resource_handle": item["resource_handle"],
            "kind": item["kind"],
            "title": item.get("title", ""),
            "usage_hint": item.get("usage_hint", ""),
        }
        for item in catalog
    ]


def _compose_creator_resource_selection_prompt() -> str:
    return (
        "你是 Creator 模式的资源按需加载助手。\n\n"
        "输入包含 Loaded SKILL.md、resource_catalog 和用户请求。\n"
        "目标：判断是否需要先读取部分资源帮助当前回答。\n\n"
        "规则：\n"
        "1. 仅能从 resource_catalog 中选择 resource_handle。\n"
        "2. 最多选择 5 个资源。\n"
        "3. 若无需资源，直接说明不需要。\n"
        "4. 输出格式不强制 JSON，可直接给结论和 resource handle 列表。\n"
    )


def _parse_creator_resource_selection_decision(
    text: str,
    *,
    resource_catalog: list[dict],
) -> dict:
    valid_handles = {
        str(item.get("resource_handle", "")).strip()
        for item in resource_catalog
        if str(item.get("resource_handle", "")).strip()
    }

    def _filter_handles(candidates: list[str]) -> list[str]:
        selected: list[str] = []
        for item in candidates:
            handle = str(item or "").strip()
            if not handle or handle not in valid_handles or handle in selected:
                continue
            selected.append(handle)
            if len(selected) >= 5:
                break
        return selected

    stripped = _strip_markdown_json_fence(text)
    parsed = None
    try:
        parsed = json.loads(stripped)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        raw_need = parsed.get("need_resources")
        raw_handles = parsed.get("resource_handles", [])
        selected = _filter_handles(raw_handles if isinstance(raw_handles, list) else [])
        if isinstance(raw_need, str):
            need_resources = raw_need.strip().lower() in {"true", "1", "yes", "y"}
        elif raw_need is None:
            need_resources = bool(selected)
        else:
            need_resources = bool(raw_need)
        if not need_resources or not selected:
            return {
                "need_resources": False,
                "resource_handles": [],
                "reason": str(parsed.get("reason") or "").strip(),
            }
        return {
            "need_resources": True,
            "resource_handles": selected,
            "reason": str(parsed.get("reason") or "").strip(),
        }

    extracted = _filter_handles(re.findall(r"resource:\d+", text or ""))
    if extracted:
        return {
            "need_resources": True,
            "resource_handles": extracted,
            "reason": "从自由文本解析到资源句柄",
        }
    return {"need_resources": False, "resource_handles": [], "reason": "未选择资源"}


async def _run_creator_resource_selection_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    resource_catalog: list[dict],
) -> dict:
    if not resource_catalog:
        return {"need_resources": False, "resource_handles": [], "reason": "无可用资源"}

    messages = [
        {"role": "system", "content": _compose_creator_resource_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "resource_catalog": _creator_resource_catalog_for_selector(resource_catalog),
                    "user_messages": _request_messages_with_files(request),
                    "last_user_text": _last_user_text(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    try:
        decision_text = await complete_chat_once(messages, _planner_model_name(model))
    except Exception:
        logger.exception("creator resource selection round failed")
        return {"need_resources": False, "resource_handles": [], "reason": "选择器调用失败"}

    return _parse_creator_resource_selection_decision(
        decision_text,
        resource_catalog=resource_catalog,
    )


def _compose_creator_loaded_resources_prompt(
    *,
    skill_name: str,
    resource_catalog: list[dict],
    selected_handles: list[str],
) -> str:
    resource_by_handle = {
        str(item.get("resource_handle")): item for item in resource_catalog if item.get("resource_handle")
    }
    sections: list[str] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(str(handle))
        if not resource:
            continue
        path = str(resource.get("path") or "").strip()
        if not path:
            continue
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
        "## Loaded On-Demand Resources (Creator)\n\n"
        "以下内容由宿主按需读取，可作为当前 Creator 回答上下文。\n\n"
        + "\n\n".join(sections)
    )


def build_kernel_skill_context() -> dict:
    """Build creator-mode skill context for the kernel Skill Creator."""
    return {
        "skill_name": "skill-creator",
        "body_loader": load_kernel_creator_body_prompt,
        "force_body": True,
        "enable_action_execution": True,
        "require_action_confirmation": False,
        "execution_root": settings.skills_path,
        "strict_skill_execution": True,
        "enable_resource_preload": True,
    }


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Multi-turn chat powered by the fixed kernel Skill Creator."""
    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream_creator(skill_context, request)


def _make_stream_creator(skill_context: dict, request: ChatRequest):
    """Creator-specific streaming with phase-aware behaviour.

    Unlike _make_stream which always runs the runtime planner, this function
    detects whether the conversation is still in Phase 1-2 (pure conversation)
    or has advanced to Phase 3+ (action execution).

    Phase detection:
    - If the LLM's response in the current turn contains the phase-3 marker,
      run the planner/executor on the generated output.
    - Otherwise, stream the LLM response directly without planning.

    This avoids the overhead and potential mis-planning of running the runtime
    planner on conversational Phase 1-2 turns that are purely asking questions.
    """
    model = request.model or settings.default_model
    _MAX_CMD_DISPLAY_LENGTH = 60
    execution_root = skill_context.get("execution_root")
    parent_skill_name = skill_context.get("skill_name", "")
    enable_resource_preload = bool(skill_context.get("enable_resource_preload", False))

    if execution_root is not None:
        execution_root = Path(execution_root).resolve()
        allowed_roots = _allowed_skill_roots()
        if not any(_is_within_sandbox(execution_root, r.resolve()) for r in allowed_roots):
            raise ValueError(
                f"execution_root '{execution_root}' is outside all allowed skill roots."
            )

    def _conversation_has_phase3(messages: list) -> bool:
        """Check whether a prior assistant message already contains the phase-3 marker."""
        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role == "assistant" and content and _CREATOR_PHASE3_MARKER in content:
                return True
        return False

    async def generate():
        try:
            # ── Step 1: Load body prompt ──────────────────────────────────
            yield _sse({"status": {"phase": "loading", "message": "加载 Skill Creator 正文…"}})
            body_prompt = skill_context["body_loader"]()
            yield _thought(
                "body_loaded",
                "加载 SKILL.md",
                f"正文已加载，共 {len(body_prompt)} 字符",
                {"body_chars": len(body_prompt), "skill_name": parent_skill_name},
            )

            # ── Step 2: Optionally preload resources ──────────────────────
            if enable_resource_preload:
                resource_catalog = _extract_creator_resource_catalog(body_prompt)
                if resource_catalog:
                    yield _sse({"status": {"phase": "loading_resources", "message": "按需加载资源…"}})
                    resource_decision = await _run_creator_resource_selection_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        resource_catalog=resource_catalog,
                    )
                    yield _thought(
                        "resource_selection",
                        "资源选择",
                        (
                            f"加载 {len(resource_decision.get('resource_handles', []))} 个资源"
                            if resource_decision.get("need_resources")
                            else f"无需加载额外资源：{resource_decision.get('reason', '')}"
                        ),
                        {
                            "need_resources": resource_decision.get("need_resources"),
                            "resource_handles": resource_decision.get("resource_handles", []),
                            "catalog_size": len(resource_catalog),
                        },
                    )

                    if resource_decision.get("need_resources"):
                        selected = resource_decision.get("resource_handles") or []
                        yield _sse({"status": {"phase": "loading_resources", "message": f"加载 {len(selected)} 个资源…"}})
                        loaded_resources_prompt = _compose_creator_loaded_resources_prompt(
                            skill_name=parent_skill_name,
                            resource_catalog=resource_catalog,
                            selected_handles=selected,
                        )
                        if loaded_resources_prompt:
                            body_prompt = body_prompt + loaded_resources_prompt

            # ── Step 3: Detect phase from conversation history ────────────
            already_in_phase3 = _conversation_has_phase3(
                getattr(request, "messages", [])
            )

            yield _thought(
                "phase_detection",
                "阶段检测",
                f"{'已进入 Phase 3+（执行阶段）' if already_in_phase3 else '仍在 Phase 1-2（对话阶段）'}",
                {"already_in_phase3": already_in_phase3},
            )

            # ── Step 4: Build messages for the LLM ────────────────────────
            final_messages: list[dict] = [{"role": "system", "content": body_prompt}]
            final_messages.extend(_request_messages_with_files(request))

            if already_in_phase3:
                # ── Phase 3+ path: run planner then execute ───────────────
                yield _sse({"status": {"phase": "planning", "message": "规划执行方案…"}})
                try:
                    runtime_plan = await _run_skill_runtime_planner_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        execution_root=execution_root,
                    )

                    mode = runtime_plan.get("mode")
                    tasks = runtime_plan.get("tasks") or []

                    yield _thought(
                        "planner_output",
                        "规划结果",
                        f"模式：{mode}，共 {len(tasks)} 个动作",
                        {
                            "mode": mode,
                            "task_count": len(tasks),
                            "tasks": [
                                {
                                    "action": t.get("action"),
                                    "command": (str(t.get("command") or ""))[:120] or None,
                                    "path": t.get("path") or t.get("resource_handle") or None,
                                    "reason": str(t.get("reason") or "")[:200],
                                }
                                for t in tasks
                            ],
                        },
                    )

                    if mode == "execute" and tasks:
                        _exec_inferred_root = _infer_skill_root_from_tasks(
                            runtime_plan, execution_root=execution_root
                        )
                        _exec_cwd = execution_root or _exec_inferred_root
                        _exec_session_dir = _extract_input_session_dir(
                            getattr(request, "input_files", []) or [], _exec_cwd
                        )

                        _exec_all_results: list[dict] = []
                        _exec_all_touched: list[Path] = []

                        for task in tasks:
                            task_action = str(task.get("action") or "").strip()

                            if task_action == "run_command":
                                cmd = str(task.get("command") or "")
                                short_cmd = cmd[:_MAX_CMD_DISPLAY_LENGTH] + (
                                    "…" if len(cmd) > _MAX_CMD_DISPLAY_LENGTH else ""
                                )
                                yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
                                yield _thought("action_start", "执行命令", short_cmd, {"action": "run_command", "command": cmd[:200]})
                            elif task_action == "write_file":
                                wf_path = str(task.get("path") or "")
                                yield _sse({"status": {"phase": "writing", "message": f"写入文件：{wf_path}"}})
                                yield _thought("action_start", "写入文件", wf_path, {"action": "write_file", "path": wf_path})
                            elif task_action == "create_directory":
                                cd_path = str(task.get("path") or "")
                                yield _sse({"status": {"phase": "creating", "message": f"创建目录：{cd_path}"}})
                                yield _thought("action_start", "创建目录", cd_path, {"action": "create_directory", "path": cd_path})
                            elif task_action == "read_resource":
                                res_path = str(task.get("path") or task.get("resource_handle") or "")
                                yield _sse({"status": {"phase": "reading", "message": f"读取资源：{res_path}"}})
                                yield _thought("action_start", "读取资源", res_path, {"action": "read_resource", "path": res_path})
                            else:
                                yield _thought("action_start", "执行动作", task_action, {"action": task_action})

                            task_result, task_touched = await asyncio.to_thread(
                                functools.partial(
                                    _execute_single_task,
                                    task,
                                    [],
                                    request,
                                    execution_root=execution_root,
                                    inferred_skill_root=_exec_inferred_root,
                                    skill_name=parent_skill_name,
                                    session_input_dir=_exec_session_dir,
                                )
                            )
                            _exec_all_results.append(task_result)
                            _exec_all_touched.extend(task_touched)

                            _safe_result = {
                                k: (v[:1000] if isinstance(v, str) else v)
                                for k, v in task_result.items()
                                if k not in {"content"}
                            }
                            success_flag = task_result.get("success", True)
                            yield _thought(
                                "action_result",
                                "操作结果",
                                f"{'成功' if success_flag else '失败'}",
                                _safe_result,
                            )

                        # Validate any newly created Skill roots.
                        for root in _find_created_skill_roots(_exec_all_touched):
                            skill_md = root / "SKILL.md"
                            if skill_md.exists():
                                _validate_skill_md(skill_md)

                        _exec_all_output_files: list[dict] = []
                        for r in _exec_all_results:
                            _exec_all_output_files.extend(r.get("output_files") or [])

                        exec_result = {
                            "executed": True,
                            "reason": "已根据结构化 action plan 逐任务执行。",
                            "plan": runtime_plan,
                            "results": _exec_all_results,
                            "logs": [],
                            "output_files": _exec_all_output_files,
                        }

                        yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
                        from .sandbox_chat import _compose_final_answer_prompt

                        _final_messages = [
                            {"role": "system", "content": _compose_final_answer_prompt()},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "loaded_skill_prompt": body_prompt,
                                        "user_messages": _request_messages_with_files(request),
                                        "plan": runtime_plan,
                                        "execution_result": exec_result,
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ]

                        yield _sse({"status": None})
                        if _exec_all_output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(_exec_all_output_files)} 个文件",
                                    "output_files": _exec_all_output_files,
                                }
                            })
                        async for chunk in stream_chat(_final_messages, model):
                            yield _sse({"content": chunk})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "ask_user":
                        yield _sse({"status": None})
                        missing = runtime_plan.get("missing") or []
                        text = "缺少必要信息，无法执行：\n" + "\n".join(f"- {item}" for item in missing) if missing else "缺少必要信息。"
                        yield _sse({"content": text})
                        yield "data: [DONE]\n\n"
                        return

                    # mode == direct_answer or not_applicable → fall through to LLM
                    yield _sse({"status": None})

                except Exception as exc:
                    logger.exception("creator runtime planning/execution failed")
                    yield _sse({"status": None})
                    yield _sse({"error": "错误：运行时规划或执行失败"})
                    yield "data: [DONE]\n\n"
                    return

            # ── Phase 1-2 (or direct_answer fallback): stream LLM directly ──
            yield _sse({"status": None})

            assistant_chunks: list[str] = []
            async for chunk in stream_chat(final_messages, model):
                assistant_chunks.append(chunk)
                yield _sse({"content": chunk})

            assistant_text = "".join(assistant_chunks)

            # If the LLM just emitted the phase-3 marker in this turn AND
            # produced action blocks, execute them via the fallback path.
            if _CREATOR_PHASE3_MARKER in assistant_text:
                try:
                    exec_result = await _plan_and_execute_generated_output(
                        assistant_text=assistant_text,
                        request=request,
                        model=model,
                        require_confirmation=False,
                        execution_root=execution_root,
                        skill_name=parent_skill_name,
                    )
                    if exec_result.get("executed"):
                        yield _sse({"content": _format_execution_report(exec_result)})
                        output_files = exec_result.get("output_files") or []
                        if output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(output_files)} 个文件",
                                    "output_files": output_files,
                                }
                            })
                except Exception as exc:
                    logger.exception("creator fallback action execution failed")
                    yield _sse({"error": "错误：后台执行失败"})

            yield "data: [DONE]\n\n"

        except Exception as exc:
            logger.exception("creator LLM stream error")
            yield _sse({"status": None})
            yield _sse({"error": _friendly_error(exc)})
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
