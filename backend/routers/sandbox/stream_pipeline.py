"""主流式管道与路由端点。"""

import asyncio
import functools
import hashlib
import json
import logging
import time as _time_module
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...config import settings
from ...services.kernel_loader import (
    load_child_skill_body_prompt,
    load_skill_body_prompt,
    load_skill_metadata_prompt,
    read_skill_resource_text,
)
from ...services.llm_proxy import complete_chat_once, stream_chat
from ...services.model_router import (
    TEXT_TASK,
    infer_sandbox_response_task,
    route_model,
)
from ...services.sandbox_session import (
    DialogIntent,
    SandboxSessionState,
    StepName,
    classify_dialog_intent,
    get_or_create_session,
)
from ..chat_utils import (
    _allowed_skill_roots,
    _extract_all_fenced_blocks,
    _extract_input_session_dir,
    _find_created_skill_roots,
    _friendly_error,
    _has_creation_confirmation,
    _is_within_sandbox,
    _last_user_text,
    _request_messages_with_files,
    _sse,
    _thought,
    _task_checklist,
    _sandbox_retry,
    _validate_skill_md,
)
from ..chat_models import ChatRequest
from .path_resolution import (
    _skill_root_for_name,
    _available_scripts_for_root,
    _infer_skill_root_from_tasks,
    _normalize_skill_resource_path,
)
from .resource_catalog import (
    _extract_runtime_resource_catalog,
    _resource_catalog_for_planner,
    _run_resource_selection_round,
)
from .resource_loader import _compose_loaded_resources_prompt
from .metadata_decisions import (
    _run_metadata_round,
    _run_child_skill_selection_round,
)
from .multimodal import _request_messages_with_inline_images
from .instruction_analysis import _run_instruction_analysis_round
from .sop_planner import (
    _generate_sop_from_plan,
    _cleanup_expired_plans,
    _pending_plans,
    _format_task_checklist_markdown,
)
from .action_schema import _build_runtime_action_schema
from .runtime_planner import _run_skill_runtime_planner_round
from .final_answer import (
    _generate_final_answer_from_observation,
    _run_block_planner_round,
)
from .workflow_detection import (
    _execution_requires_run_command_observation,
    _has_successful_run_command_observation,
)
from .task_executor import (
    _execute_single_task,
    _execute_planned_actions,
)
from .output_links import _finalize_answer_output_file_links
from .stdout_render import (
    _render_success_stdout_payload,
    _format_execution_report,
)
from .legacy_fallback import _plan_and_execute_generated_output
from .workflow_dataflow import (
    _execute_skill_workflow,
    _workflow_context_from_request_text,
)
from .error_correction import (
    _MAX_SANDBOX_RETRY,
    _get_llm_error_correction,
    _apply_error_correction,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _step_skipped(step: StepName, reason: str) -> str:
    """Build a 'step_skipped' SSE event to notify the frontend that a
    pipeline step was skipped because its output was already cached."""
    return _sse({
        "type": "step_skipped",
        "data": {
            "step": step.value,
            "reason": reason,
            "ts": _time_module.time(),
        },
    })


def _make_stream(skill_context: dict, request: ChatRequest):
    """Staged Skill execution with shared runtime planning and action execution."""
    requested_model = request.model or settings.default_model
    model = route_model(TEXT_TASK, requested_model=requested_model, reason="sandbox default response").model
    _MAX_CMD_DISPLAY_LENGTH = 60
    force_body = bool(skill_context.get("force_body", False))
    enable_action_execution = bool(skill_context.get("enable_action_execution", False))
    require_action_confirmation = bool(skill_context.get("require_action_confirmation", True))
    strict_skill_execution = bool(skill_context.get("strict_skill_execution", False))
    execution_root = skill_context.get("execution_root")
    child_body_loader = skill_context.get("child_body_loader")
    parent_skill_name = skill_context.get("skill_name", "")
    enable_resource_preload = bool(skill_context.get("enable_resource_preload", False))

    # Dual execution mode: "plan" (规划模式，预览后确认再执行) or "execute" (执行模式，直接执行)
    # Backward compatible: "craft" is mapped to "execute" via effective_execution_mode()
    execution_mode = request.effective_execution_mode()

    if execution_root is not None:
        execution_root = Path(execution_root).resolve()
        # Verify the resolved path is within an allowed skill root so that
        # a crafted skill_context cannot steer execution outside the sandbox.
        allowed_roots = _allowed_skill_roots()
        if not any(_is_within_sandbox(execution_root, r.resolve()) for r in allowed_roots):
            raise ValueError(
                f"execution_root '{execution_root}' is outside all allowed skill roots."
            )

    async def generate():
        try:
            # Track resource loading status across rounds
            loaded_resource_paths: list[str] = []
            failed_resource_paths: list[dict] = []

            # --- Step-skipping: resolve session state & intent ---
            session_state: SandboxSessionState | None = None
            intent = DialogIntent.NEW_TASK
            if request.sandbox_session_id:
                session_state = get_or_create_session(
                    request.sandbox_session_id, parent_skill_name
                )
                intent = classify_dialog_intent(request.messages)
                logger.debug(
                    "sandbox step-skip: session=%s intent=%s completed_steps=%s",
                    request.sandbox_session_id,
                    intent.value,
                    session_state.completed_steps,
                )

                # If new files were uploaded, invalidate cached resource/body state
                if getattr(request, "input_files", None):
                    logger.debug("sandbox step-skip: input_files detected, invalidating cache")
                    session_state.invalidate()
                    intent = DialogIntent.NEW_TASK

            if force_body:
                need_body = True
                logger.debug("force_body=True, skip metadata decision and load SKILL.md body directly")
            elif session_state and session_state.should_skip(StepName.METADATA, intent):
                # --- SKIP: metadata round ---
                need_body = session_state.need_body  # type: ignore[assignment]
                yield _step_skipped(StepName.METADATA, "复用上一轮匹配度分析结果")
                yield _thought(
                    "metadata_decision",
                    "分析匹配度（跳过）",
                    f"复用缓存：{'需要加载正文' if need_body else '请求与 Skill 不匹配，跳过正文'}",
                    {
                        "need_body": need_body,
                        "skipped": True,
                    },
                )
            else:
                yield _sse({"status": {"phase": "analyzing", "message": "分析请求匹配度…"}})
                need_body = await _run_metadata_round(
                    metadata_prompt=skill_context["metadata_prompt"],
                    request=request,
                    model=model,
                )
                yield _thought(
                    "metadata_decision",
                    "分析匹配度",
                    f"{'需要加载正文' if need_body else '请求与 Skill 不匹配，跳过正文'}",
                    {
                        "need_body": need_body,
                        "metadata_chars": len(skill_context.get("metadata_prompt", "")),
                    },
                )
                # Cache the result
                if session_state:
                    session_state.need_body = need_body
                    session_state.cache_artifact(StepName.METADATA, need_body)

            if not need_body:
                yield _sse({"status": None})
                fallback_messages = [
                    {
                        "role": "system",
                        "content": (
                            "当前用户请求与已选 Skill 及其子 Skill 的 metadata 不匹配。"
                            "请简短说明该 Skill 不适用，并提示用户重新描述需求。"
                        ),
                    }
                ]
                fallback_messages.extend(_request_messages_with_files(request))

                async for chunk in stream_chat(fallback_messages, model):
                    yield _sse({"content": chunk})

                yield "data: [DONE]\n\n"
                return

            if session_state and session_state.should_skip(StepName.LOAD_BODY, intent):
                # --- SKIP: body loading ---
                body_prompt = session_state.body_prompt or skill_context["body_loader"]()
                yield _step_skipped(StepName.LOAD_BODY, "复用上一轮 Skill 正文")
                yield _thought(
                    "body_loaded",
                    "加载 SKILL.md（跳过）",
                    f"复用缓存正文，共 {len(body_prompt)} 字符",
                    {
                        "body_chars": len(body_prompt),
                        "skill_name": parent_skill_name,
                        "skipped": True,
                    },
                )
            else:
                yield _sse({"status": {"phase": "loading", "message": "加载 Skill 正文…"}})
                body_prompt = skill_context["body_loader"]()
                yield _thought(
                    "body_loaded",
                    "加载 SKILL.md",
                    f"正文已加载，共 {len(body_prompt)} 字符",
                    {
                        "body_chars": len(body_prompt),
                        "skill_name": parent_skill_name,
                    },
                )
                # Cache the result
                if session_state:
                    session_state.body_prompt = body_prompt
                    session_state.cache_artifact(StepName.LOAD_BODY, body_prompt)

            if child_body_loader:
                if session_state and session_state.should_skip(StepName.CHILD_SKILL, intent):
                    # --- SKIP: child skill selection ---
                    child_decision = session_state.child_decision or {"need_child": False, "reason": "缓存无子 Skill"}
                    yield _step_skipped(StepName.CHILD_SKILL, "复用上一轮子 Skill 选择结果")
                    yield _thought(
                        "child_decision",
                        "子 Skill 检查（跳过）",
                        (
                            f"复用缓存：加载子 Skill：{child_decision.get('child_ref')}"
                            if child_decision.get("need_child")
                            else f"复用缓存：无需子 Skill"
                        ),
                        {
                            "need_child": child_decision.get("need_child"),
                            "child_ref": child_decision.get("child_ref", ""),
                            "reason": child_decision.get("reason", ""),
                            "skipped": True,
                        },
                    )
                else:
                    yield _sse({"status": {"phase": "loading_child", "message": "检查子 Skill…"}})
                    child_decision = await _run_child_skill_selection_round(
                        parent_metadata_prompt=skill_context["metadata_prompt"],
                        request=request,
                        model=model,
                    )
                    yield _thought(
                        "child_decision",
                        "子 Skill 检查",
                        (
                            f"加载子 Skill：{child_decision.get('child_ref')}"
                            if child_decision.get("need_child")
                            else f"无需子 Skill：{child_decision.get('reason', '')}"
                        ),
                        {
                            "need_child": child_decision.get("need_child"),
                            "child_ref": child_decision.get("child_ref", ""),
                            "reason": child_decision.get("reason", ""),
                        },
                    )
                    # Cache the result
                    if session_state:
                        session_state.child_decision = child_decision
                        session_state.cache_artifact(StepName.CHILD_SKILL, child_decision)

                if child_decision.get("need_child"):
                    child_ref = child_decision.get("child_ref", "")
                    yield _sse({"status": {"phase": "loading_child", "message": f"加载子 Skill：{child_ref}…"}})
                    try:
                        child_body_prompt = child_body_loader(child_ref)
                        body_prompt = (
                            f"{body_prompt}\n\n"
                            "---\n\n"
                            "## Loaded Child Skill Body\n\n"
                            f"父 Skill 已根据用户请求按需加载子 Skill：`{child_ref}`。\n"
                            "下面是该子 Skill 的完整执行正文。\n\n"
                            f"{child_body_prompt}"
                        )
                    except Exception as exc:
                        logger.warning(
                            "failed to load child skill body parent=%s child_ref=%s error=%s",
                            parent_skill_name,
                            child_ref,
                            exc,
                        )
                        body_prompt = (
                            f"{body_prompt}\n\n"
                            "---\n\n"
                            "## Child Skill Load Warning\n\n"
                            f"运行时尝试加载子 Skill `{child_ref}`，但加载失败：{exc}\n"
                            "请不要假装已经读取该子 Skill 正文。"
                        )

            if enable_resource_preload:
                if session_state and session_state.should_skip(StepName.RESOURCES, intent):
                    # --- SKIP: resource selection ---
                    resource_decision = session_state.resource_decision or {"need_resources": False, "reason": "缓存无资源"}
                    # Re-apply previously loaded resources to body_prompt
                    if session_state.augmented_body_prompt:
                        body_prompt = session_state.augmented_body_prompt
                    yield _step_skipped(StepName.RESOURCES, "复用上一轮资源选择结果")
                    yield _thought(
                        "resource_selection",
                        "资源选择（跳过）",
                        (
                            f"复用缓存：加载 {len(resource_decision.get('resource_handles', []))} 个资源"
                            if resource_decision.get("need_resources")
                            else "复用缓存：无需加载额外资源"
                        ),
                        {
                            "need_resources": resource_decision.get("need_resources"),
                            "resource_handles": resource_decision.get("resource_handles", []),
                            "reason": resource_decision.get("reason", ""),
                            "skipped": True,
                        },
                    )
                else:
                    resource_catalog = _extract_runtime_resource_catalog(body_prompt)
                    if resource_catalog:
                        yield _sse({"status": {"phase": "loading_resources", "message": "按需加载资源…"}})
                    resource_decision = await _run_resource_selection_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        resource_catalog=resource_catalog,
                    )
                    yield _thought(
                        "resource_selection",
                        "资源选择",
                        (
                            f"加载 {len(resource_decision.get('resource_handles', []))} 个资源：{', '.join(resource_decision.get('resource_handles', []))}"
                            if resource_decision.get("need_resources")
                            else f"无需加载额外资源：{resource_decision.get('reason', '')}"
                        ),
                        {
                            "need_resources": resource_decision.get("need_resources"),
                            "resource_handles": resource_decision.get("resource_handles", []),
                            "catalog_size": len(resource_catalog),
                            "reason": resource_decision.get("reason", ""),
                        },
                    )

                    if resource_decision.get("need_resources"):
                        selected = resource_decision.get("resource_handles") or []
                        yield _sse({"status": {"phase": "loading_resources", "message": f"加载 {len(selected)} 个资源…"}})
                        loaded_result = _compose_loaded_resources_prompt(
                            skill_name=parent_skill_name,
                            resource_catalog=resource_catalog,
                            selected_handles=selected,
                            execution_root=execution_root,
                        )

                        loaded_resource_paths = loaded_result.get("loaded_paths", [])
                        failed_resource_paths = loaded_result.get("failed_paths", [])
                        loaded_resources_prompt = loaded_result.get("prompt", "")
                        if loaded_resources_prompt:
                            body_prompt = body_prompt + loaded_resources_prompt

                    # Cache the result
                    if session_state:
                        session_state.resource_decision = resource_decision
                        session_state.augmented_body_prompt = body_prompt
                        session_state.cache_artifact(StepName.RESOURCES, resource_decision)

            # Append uploaded input-file context to the body prompt so the LLM
            # knows which files are available. For small text files the content is
            # embedded directly so the LLM can reason about the data without running
            # a script first. Binary or large files are described by path only.
            if getattr(request, "input_files", None):
                _TEXT_CONTENT_SUFFIXES = frozenset({
                    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl",
                    ".yaml", ".yml", ".xml", ".html", ".htm", ".log",
                })
                _MAX_INLINE_BYTES = 100 * 1024  # 100 KB

                file_sections: list[str] = []
                for f in request.input_files:
                    rel_path = f.get("path", "")
                    filename = f.get("filename", rel_path.split("/")[-1] if rel_path else "")
                    suffix = Path(filename).suffix.lower() if filename else ""

                    # Try to read text content for embedding
                    content_block = ""
                    if rel_path and parent_skill_name and suffix in _TEXT_CONTENT_SUFFIXES:
                        try:
                            abs_path = (settings.skills_path / parent_skill_name / rel_path).resolve()
                            # Ensure path stays inside the skill directory
                            skill_dir_check = (settings.skills_path / parent_skill_name).resolve()
                            abs_path.relative_to(skill_dir_check)
                            if abs_path.is_file():
                                raw = abs_path.read_bytes()
                                if len(raw) <= _MAX_INLINE_BYTES:
                                    text = raw.decode("utf-8", errors="replace")
                                    # Choose a fence that doesn't appear in the content.
                                    # Prefer ``` but fall back to a tilde fence when the
                                    # file itself contains triple-backtick sequences.
                                    if "```" not in text:
                                        fence, content_text = "```", text
                                    else:
                                        fence = "~~~~"
                                        content_text = text.replace("~~~~", "~ ~ ~ ~")
                                    content_block = (
                                        f"\n\n  文件内容如下：\n\n  {fence}\n{content_text}\n  {fence}"
                                    )
                        except Exception:
                            pass  # fall back to path-only if read fails

                    if content_block:
                        file_sections.append(
                            f"- `{rel_path}`（文件名：`{filename}`）{content_block}"
                        )
                    else:
                        # Strip the leading "inputs/" component so the script only needs
                        # os.path.join(INPUT_DIR, remaining) — INPUT_DIR points to inputs/.
                        try:
                            _rel_path_obj = Path(rel_path)
                            # Use parts[0] to avoid Windows backslash ambiguity.
                            if _rel_path_obj.parts and _rel_path_obj.parts[0] == "inputs":
                                rel_to_input_dir = Path(*_rel_path_obj.parts[1:]).as_posix()
                            else:
                                rel_to_input_dir = rel_path
                        except (ValueError, IndexError):
                            rel_to_input_dir = rel_path
                        file_sections.append(
                            f"- `{rel_path}`（文件名：`{filename}`）"
                            f"脚本可通过 `os.path.join(os.environ['INPUT_DIR'], '{rel_to_input_dir}')` 读取，"
                            "或直接用 `os.environ['INPUT_SESSION_DIR']` 目录（该目录下包含本次会话所有上传文件）"
                            "。"
                        )

                if file_sections:
                    sections_text = "\n".join(file_sections)
                    body_prompt = (
                        body_prompt
                        + "\n\n---\n\n"
                        "## 当前对话已上传文件\n\n"
                        "用户在本次对话中上传了以下文件，你必须以这些文件为输入进行分析或处理。\n"
                        "- 对于文本/数据文件，内容已直接展示在下方，请直接阅读并回答。\n"
                        "- 需要执行计算、统计、转换等操作时，可生成 Python 脚本并运行，"
                        "脚本中使用 `os.environ['INPUT_SESSION_DIR']` 获取上传文件目录，"
                        "使用 `os.environ['OUTPUT_DIR']` 输出结果文件。\n\n"
                        f"{sections_text}\n"
                    )

            if enable_action_execution:
                # --- Instruction Analysis Round ---
                yield _sse({"status": {"phase": "analyzing_instruction", "message": "分析指令语义…"}})
                instruction_analysis = await _run_instruction_analysis_round(
                    body_prompt=body_prompt,
                    request=request,
                    model=model,
                )
                yield _thought(
                    "instruction_analysis",
                    "指令语义分析",
                    f"意图：{instruction_analysis.get('intent', '')[:80]}，复杂度：{instruction_analysis.get('complexity', '')}",
                    instruction_analysis,
                )

                # --- Runtime Planner Round ---
                try:
                    yield _sse({"status": {"phase": "planning", "message": "规划执行方案…"}})
                    runtime_plan = await _run_skill_runtime_planner_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        execution_root=execution_root,
                        skill_name=parent_skill_name,
                        loaded_paths=loaded_resource_paths,
                        failed_paths=failed_resource_paths,
                    )

                    response_route = route_model(
                        infer_sandbox_response_task(
                            body_prompt=body_prompt,
                            user_text=_last_user_text(request),
                            plan=runtime_plan,
                        ),
                        requested_model=requested_model,
                        reason="sandbox runtime plan classification",
                    )
                    response_model = response_route.model
                    yield _sse({"model_ack": response_route.ack()})

                    mode = runtime_plan.get("mode")
                    tasks = runtime_plan.get("tasks") or []

                    # Emit planner_output thought with safe task summaries (no SKILL.md content).
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
                            "errors": runtime_plan.get("errors") or [],
                            "missing": runtime_plan.get("missing") or [],
                        },
                    )

                    # --- Generate SOP document ---
                    sop_document = _generate_sop_from_plan(
                        instruction_analysis=instruction_analysis,
                        runtime_plan=runtime_plan,
                        skill_name=parent_skill_name,
                    )
                    yield _sse({"sop_plan": sop_document})
                    yield _thought(
                        "sop_generated",
                        "SOP 方案",
                        f"共 {sop_document.get('total_steps', 0)} 个步骤",
                        {"title": sop_document.get("title", ""), "total_steps": sop_document.get("total_steps", 0)},
                    )

                    # --- Plan Mode: preview and await confirmation ---
                    if execution_mode == "plan" and mode == "execute" and tasks:
                        plan_id = hashlib.sha256(
                            f"{parent_skill_name}:{_time_module.time()}:{_last_user_text(request)[:100]}".encode()
                        ).hexdigest()[:16]

                        _cleanup_expired_plans()
                        _pending_plans[plan_id] = {
                            "plan": runtime_plan,
                            "instruction_analysis": instruction_analysis,
                            "sop": sop_document,
                            "skill_context": skill_context,
                            "request": request,
                            "ts": _time_module.time(),
                        }

                        # Build task items for both plan_preview and task_checklist events
                        plan_tasks = [
                            {
                                "action": t.get("action"),
                                "command": (str(t.get("command") or ""))[:200] or None,
                                "path": t.get("path") or t.get("resource_handle") or None,
                                "reason": str(t.get("reason") or "")[:300],
                            }
                            for t in tasks
                        ]

                        yield _sse({
                            "plan_preview": {
                                "plan_id": plan_id,
                                "mode": mode,
                                "instruction_analysis": instruction_analysis,
                                "sop": sop_document,
                                "tasks": plan_tasks,
                                "total_tasks": len(tasks),
                                "awaiting_confirmation": True,
                            }
                        })

                        # Push inline task checklist for display in the chat bubble
                        checklist_tasks = [
                            {
                                "index": idx,
                                "action": t.get("action"),
                                "description": str(t.get("reason") or ""),
                                "command": (str(t.get("command") or ""))[:200] or None,
                                "path": t.get("path") or t.get("resource_handle") or None,
                            }
                            for idx, t in enumerate(tasks)
                        ]
                        yield _task_checklist(checklist_tasks)

                        # Also push Markdown checklist as content for backward compatibility
                        checklist_md = _format_task_checklist_markdown(
                            tasks, instruction_analysis=instruction_analysis
                        )
                        yield _sse({"status": None})
                        yield _sse({"content": (
                            f"📋 **执行方案已生成**（共 {len(tasks)} 个步骤）\n\n"
                            f"{checklist_md}\n\n"
                            "请在左侧面板查看详细方案，确认后将开始执行。\n"
                            f"（方案ID：`{plan_id}`）"
                        )})
                        yield "data: [DONE]\n\n"
                        return

                    # --- execute_workflow mode: deterministic multi-script execution ---
                    if mode == "execute_workflow":
                        try:
                            yield _sse({"status": {"phase": "executing_workflow", "message": "执行工作流…"}})
                            action_schema = _build_runtime_action_schema(body_prompt, execution_root=execution_root)
                            if action_schema.get("errors"):
                                raise ValueError("Skill Action schema 校验失败: " + json.dumps(action_schema["errors"], ensure_ascii=False))

                            user_context = _workflow_context_from_request_text(
                                _last_user_text(request),
                                first_entry=(action_schema.get("entries") or [{}])[0] if action_schema.get("entries") else {},
                            )
                            workflow_result = await _execute_skill_workflow(
                                execution_root=execution_root,
                                action_schema=action_schema,
                                user_context=user_context,
                                request=request,
                                skill_name=parent_skill_name,
                            )

                            _exec_all_output_files = workflow_result.get("output_files") or []
                            _loaded_resource_paths = workflow_result.get("loaded_resource_paths") or []
                            _failed_resource_paths = workflow_result.get("failed_resource_paths") or []
                            _planned_followup_commands = workflow_result.get("planned_followup_commands") or []

                            yield _thought(
                                "workflow_result",
                                "工作流执行结果",
                                f"完成 {len(workflow_result.get('results', []))} 步，"
                                f"输出 {len(_exec_all_output_files)} 文件",
                                {
                                    "step_count": len(workflow_result.get("results", [])),
                                    "output_file_count": len(_exec_all_output_files),
                                    "loaded_resource_paths": _loaded_resource_paths,
                                    "failed_resource_paths": _failed_resource_paths,
                                    "planned_followup_commands": _planned_followup_commands,
                                },
                            )

                            yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
                            final_answer = await _generate_final_answer_from_observation(
                                body_prompt=body_prompt,
                                request=request,
                                model=response_model,
                                plan=runtime_plan,
                                execution_result=workflow_result,
                            )
                            final_answer = _finalize_answer_output_file_links(final_answer, _exec_all_output_files)
                            yield _thought(
                                "final_answer",
                                "生成回答",
                                f"共 {len(final_answer)} 字符，包含 {len(_exec_all_output_files)} 个输出文件",
                                {
                                    "answer_chars": len(final_answer),
                                    "has_output_files": bool(_exec_all_output_files),
                                    "output_file_count": len(_exec_all_output_files),
                                },
                            )

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

                            yield _sse({"content": final_answer})
                            yield "data: [DONE]\n\n"
                            return

                        except Exception as exc:
                            logger.exception("workflow execution failed: %s", exc)
                            yield _sse({"status": None})
                            yield _sse({"error": "错误：工作流执行失败"})
                            yield "data: [DONE]\n\n"
                            return

                    if mode == "execute" and tasks:
                        # Set up shared execution context for the per-task loop.
                        _exec_inferred_root = _infer_skill_root_from_tasks(
                            runtime_plan, execution_root=execution_root
                        )
                        _exec_cwd = execution_root or _exec_inferred_root
                        _exec_session_dir = _extract_input_session_dir(
                            getattr(request, "input_files", []) or [], _exec_cwd
                        )

                        _exec_all_results: list[dict] = []
                        _exec_all_touched: list[Path] = []
                        _exec_completed_indices: list[int] = []
                        _exec_accumulated_output_files: list[dict] = []  # output_files from prior tasks
                        _loaded_resource_paths: list[str] = []
                        _failed_resource_paths: list[str] = []
                        _planned_followup_commands: list[str] = []

                        # --- Progressive Resource Disclosure (需求): 渐进式披露 ---
                        # Resource cache: maps resource_handle/path to loaded content.
                        # Resources are loaded on-demand per task rather than all upfront.
                        _resource_cache: dict[str, str] = {}
                        _resource_catalog = _extract_runtime_resource_catalog(
                            body_prompt, execution_root=execution_root
                        ) if execution_root else []

                        def _load_resource_for_task(t: dict) -> str | None:
                            """Load a resource on-demand for a specific task.

                            Returns the loaded content, or None if no resource needed.
                            Caches results to avoid redundant reads.
                            """
                            handle = str(t.get("resource_handle") or "").strip()
                            rel_path = str(t.get("path") or "").strip()

                            # Check cache first
                            cache_key = handle or rel_path
                            if cache_key and cache_key in _resource_cache:
                                return _resource_cache[cache_key]

                            # Load from disk if not cached
                            if rel_path and parent_skill_name:
                                try:
                                    observation = read_skill_resource_text(
                                        parent_skill_name, rel_path,
                                        max_chars=settings.skill_resource_max_chars,
                                    )
                                    content = observation.get("content", "")
                                    if cache_key:
                                        _resource_cache[cache_key] = content
                                    return content
                                except Exception:
                                    pass

                            return None

                        # Push initial task checklist for execute mode
                        exec_checklist_tasks = [
                            {
                                "index": idx,
                                "action": t.get("action"),
                                "description": str(t.get("reason") or ""),
                                "command": (str(t.get("command") or ""))[:200] or None,
                                "path": t.get("path") or t.get("resource_handle") or None,
                            }
                            for idx, t in enumerate(tasks)
                        ]
                        yield _task_checklist(exec_checklist_tasks, completed_indices=[], executing_index=-1)

                        # Execute tasks one at a time so the frontend receives
                        # real-time thought events after each task completes.
                        for task_idx, task in enumerate(tasks):
                            task_action = str(task.get("action") or "").strip()
                            current_task = task  # may be modified by retry logic

                            # Announce what is about to happen.
                            if task_action == "run_command":
                                cmd = str(current_task.get("command") or "")
                                short_cmd = cmd[:_MAX_CMD_DISPLAY_LENGTH] + (
                                    "…" if len(cmd) > _MAX_CMD_DISPLAY_LENGTH else ""
                                )
                                yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
                                yield _thought(
                                    "action_start",
                                    "执行命令",
                                    short_cmd,
                                    {"action": "run_command", "command": cmd[:200]},
                                )
                                # Auto-detect sandbox execution requirement (需求)
                                # When run_command is detected, automatically flag as sandbox execution
                                if instruction_analysis.get("requires_script_execution"):
                                    yield _thought(
                                        "sandbox_auto_detect",
                                        "沙箱自动检测",
                                        "检测到脚本执行需求，自动调用沙箱环境",
                                        {
                                            "requires_script_execution": True,
                                            "execution_root": str(execution_root) if execution_root else None,
                                            "auto_injected_env": [
                                                "EXECUTION_ROOT", "OUTPUT_DIR", "INPUT_DIR", "INPUT_SESSION_DIR",
                                            ],
                                        },
                                    )
                            elif task_action == "read_resource":
                                res_path = str(current_task.get("path") or current_task.get("resource_handle") or "")
                                yield _sse({"status": {"phase": "reading", "message": f"读取资源：{res_path}"}})
                                yield _thought(
                                    "action_start",
                                    "读取资源",
                                    res_path,
                                    {"action": "read_resource", "path": res_path},
                                )
                            elif task_action == "write_file":
                                wf_path = str(current_task.get("path") or "")
                                yield _sse({"status": {"phase": "writing", "message": f"写入文件：{wf_path}"}})
                                yield _thought(
                                    "action_start",
                                    "写入文件",
                                    wf_path,
                                    {"action": "write_file", "path": wf_path},
                                )
                            elif task_action == "create_directory":
                                cd_path = str(current_task.get("path") or "")
                                yield _sse({"status": {"phase": "creating", "message": f"创建目录：{cd_path}"}})
                                yield _thought(
                                    "action_start",
                                    "创建目录",
                                    cd_path,
                                    {"action": "create_directory", "path": cd_path},
                                )
                            else:
                                yield _thought(
                                    "action_start",
                                    "执行动作",
                                    task_action,
                                    {"action": task_action},
                                )

                            # --- LLM Feedback Retry Loop ---
                            # When a task fails, feed the error back to the LLM
                            # for correction and retry up to _MAX_SANDBOX_RETRY times.
                            task_result = {}
                            task_touched = []

                            # --- Progressive Resource Loading ---
                            # For read_resource tasks, load on-demand with cache
                            if task_action == "read_resource":
                                loaded = _load_resource_for_task(current_task)
                                if loaded is not None:
                                    _loaded_resource_paths.append(
                                        str(current_task.get("path") or current_task.get("resource_handle") or "")
                                    )
                                    yield _thought(
                                        "resource_on_demand",
                                        "按需加载资源",
                                        f"已加载资源（{len(loaded)} 字符）",
                                        {
                                            "path": current_task.get("path", ""),
                                            "cached": current_task.get("resource_handle") or current_task.get("path") in _resource_cache,
                                        },
                                    )
                                else:
                                    _failed_resource_paths.append(
                                        str(current_task.get("path") or current_task.get("resource_handle") or "")
                                    )

                            for retry_attempt in range(_MAX_SANDBOX_RETRY + 1):
                                # Run the task in a thread and capture the result.
                                task_result, task_touched = await asyncio.to_thread(
                                    functools.partial(
                                        _execute_single_task,
                                        current_task,
                                        [],
                                        request,
                                        execution_root=execution_root,
                                        inferred_skill_root=_exec_inferred_root,
                                        skill_name=parent_skill_name,
                                        session_input_dir=_exec_session_dir,
                                        previous_output_files=_exec_accumulated_output_files or None,
                                    )
                                )

                                success_flag = task_result.get("success", True)

                                # If successful or last attempt, break the retry loop
                                if success_flag or retry_attempt >= _MAX_SANDBOX_RETRY:
                                    break

                                # Task failed — attempt LLM-based error correction
                                yield _thought(
                                    "sandbox_retry",
                                    f"执行失败，尝试修正 ({retry_attempt + 1}/{_MAX_SANDBOX_RETRY})",
                                    str(task_result.get("message") or task_result.get("stderr") or "")[:200],
                                    {
                                        "attempt": retry_attempt + 1,
                                        "max_retries": _MAX_SANDBOX_RETRY,
                                        "action": task_action,
                                    },
                                )
                                yield _sandbox_retry(
                                    attempt=retry_attempt + 1,
                                    max_retries=_MAX_SANDBOX_RETRY,
                                    error=str(task_result.get("stderr") or task_result.get("message") or "")[:500],
                                    corrected=False,
                                )

                                # Call LLM for error correction
                                correction = await _get_llm_error_correction(
                                    task=current_task,
                                    error_result=task_result,
                                    attempt=retry_attempt + 1,
                                    max_retries=_MAX_SANDBOX_RETRY,
                                    body_prompt=body_prompt,
                                    model=model,
                                )

                                if not correction.get("corrected"):
                                    # LLM could not suggest a correction, stop retrying
                                    yield _thought(
                                        "sandbox_retry",
                                        "无法修正",
                                        correction.get("reason", "LLM 无法提供修正建议"),
                                        {"corrected": False, "reason": correction.get("reason")},
                                    )
                                    break

                                # Apply the correction and retry
                                current_task = _apply_error_correction(current_task, correction)
                                yield _sandbox_retry(
                                    attempt=retry_attempt + 1,
                                    max_retries=_MAX_SANDBOX_RETRY,
                                    error=str(task_result.get("stderr") or task_result.get("message") or "")[:500],
                                    corrected=True,
                                )
                                yield _thought(
                                    "sandbox_retry",
                                    "已修正，重新执行",
                                    correction.get("reason", ""),
                                    {"corrected": True, "reason": correction.get("reason")},
                                )

                            # End of retry loop — record the final result
                            _exec_all_results.append(task_result)
                            _exec_all_touched.extend(task_touched)
                            _exec_completed_indices.append(task_idx)

                            # Collect output files for subsequent tasks to reference
                            if task_result.get("output_files"):
                                _exec_accumulated_output_files.extend(task_result["output_files"])

                            # Track planned followup commands
                            if task_action == "run_command" and task_result.get("success"):
                                _planned_followup_commands.append(
                                    str(current_task.get("command") or "")
                                )

                            # Build safe result data for the thought (truncate stdout/stderr).
                            _safe_result = {
                                k: (v[:1000] if isinstance(v, str) else v)
                                for k, v in task_result.items()
                                if k not in {"content"}  # omit large resource content
                            }

                            success_flag = task_result.get("success", True)
                            if task_action == "run_command":
                                rc = task_result.get("returncode", 0)
                                yield _thought(
                                    "action_result",
                                    "执行结果",
                                    f"{'成功' if success_flag else '失败'} exit={rc}",
                                    _safe_result,
                                )
                            elif task_action == "read_resource":
                                yield _thought(
                                    "action_result",
                                    "读取结果",
                                    f"{'成功' if success_flag else '失败'}，"
                                    f"{len(task_result.get('content', ''))} 字符",
                                    _safe_result,
                                )
                            else:
                                yield _thought(
                                    "action_result",
                                    "操作结果",
                                    f"{'成功' if success_flag else '失败'}",
                                    _safe_result,
                                )

                            # Push task_progress and updated checklist for real-time visualization
                            yield _sse({
                                "task_progress": {
                                    "executing_index": task_idx + 1 if task_idx < len(tasks) - 1 else -1,
                                    "completed_indices": list(_exec_completed_indices),
                                }
                            })
                            yield _task_checklist(
                                exec_checklist_tasks,
                                completed_indices=list(_exec_completed_indices),
                                executing_index=task_idx + 1 if task_idx < len(tasks) - 1 else -1,
                            )

                        # Post-loop: validate any newly created Skill roots.
                        for root in _find_created_skill_roots(_exec_all_touched):
                            skill_md = root / "SKILL.md"
                            if skill_md.exists():
                                _validate_skill_md(skill_md)

                        # Assemble exec_result compatible with _generate_final_answer_from_observation.
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
                            "loaded_resource_paths": _loaded_resource_paths,
                            "failed_resource_paths": _failed_resource_paths,
                            "planned_followup_commands": _planned_followup_commands,
                        }

                        # Guard: if the plan expected a run_command observation but none succeeded,
                        # skip the final LLM answer and inform the user instead.
                        if (
                            _execution_requires_run_command_observation(runtime_plan)
                            and not _has_successful_run_command_observation(_exec_all_results)
                        ):
                            yield _sse({"content": "已完成前置资源读取，但本轮没有获得成功的 run_command observation，无法生成最终回答。请检查脚本是否正确执行。"})
                            yield "data: [DONE]\n\n"
                            return

                        yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
                        # Fast path: try rendering stdout directly without LLM call
                        final_answer = _render_success_stdout_payload(exec_result)
                        if final_answer is None:
                            final_answer = await _generate_final_answer_from_observation(
                                body_prompt=body_prompt,
                                request=request,
                                model=response_model,
                                plan=runtime_plan,
                                execution_result=exec_result,
                            )
                        final_answer = _finalize_answer_output_file_links(final_answer, _exec_all_output_files)
                        yield _thought(
                            "final_answer",
                            "生成回答",
                            f"共 {len(final_answer)} 字符，包含 {len(_exec_all_output_files)} 个输出文件",
                            {
                                "answer_chars": len(final_answer),
                                "has_output_files": bool(_exec_all_output_files),
                                "output_file_count": len(_exec_all_output_files),
                            },
                        )

                        yield _sse({"status": None})

                        # Emit structured output_files event so the frontend can
                        # render download links without relying on LLM text parsing.
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

                        yield _sse({"content": final_answer})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "ask_user":
                        yield _sse({"status": None})
                        missing = runtime_plan.get("missing") or []
                        errors = runtime_plan.get("errors") or []

                        if missing:
                            text = "缺少必要信息，无法执行 Skill：\n" + "\n".join(
                                f"- {item}" for item in missing
                            )
                        elif errors:
                            text = "运行时规划失败：\n" + "\n".join(
                                f"- {json.dumps(item, ensure_ascii=False)}" for item in errors
                            )
                        else:
                            text = "缺少必要信息，无法执行当前 Skill。"

                        yield _sse({"content": text})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "not_applicable":
                        yield _sse({"status": None})
                        yield _sse({"content": "当前用户请求与该 Skill 不匹配，请重新选择 Skill 或重新描述需求。"})
                        yield "data: [DONE]\n\n"
                        return

                    # mode == direct_answer 时继续走普通主模型回答。
                    yield _sse({"status": None})

                except Exception as exc:
                    logger.exception("runtime skill action planning/execution failed")
                    yield _sse({"status": None})
                    yield _sse({"error": "错误：运行时规划或执行失败"})
                    yield "data: [DONE]\n\n"
                    return

            response_route = route_model(
                infer_sandbox_response_task(
                    body_prompt=body_prompt,
                    user_text=_last_user_text(request),
                    plan=locals().get("runtime_plan") if isinstance(locals().get("runtime_plan"), dict) else None,
                    input_files=request.input_files,
                ),
                requested_model=requested_model,
                reason="sandbox final response classification",
            )
            response_model = response_route.model
            yield _sse({"model_ack": response_route.ack()})

            final_messages: list[dict] = []
            final_messages.append(
                {
                    "role": "system",
                    "content": body_prompt,
                }
            )

            _runtime_plan_for_final = locals().get("runtime_plan")
            if isinstance(_runtime_plan_for_final, dict):
                _final_instruction = str(_runtime_plan_for_final.get("final_instruction") or "").strip()
                if _final_instruction:
                    final_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "运行时动作意图判断器给出的本轮执行提示：\n"
                                f"{_final_instruction}\n\n"
                                "如果该提示要求输出可执行动作，必须把真实命令或文件内容放入 fenced code block，"
                                "后台只会执行本轮回复中已经出现的 fenced code block。"
                            ),
                        }
                    )

            if strict_skill_execution:
                final_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "当前处于沙盒 Skill 严格执行模式。\n\n"
                            "你必须严格遵循已加载的 Loaded SKILL.md，禁止把它当作普通参考资料。\n"
                            "你不得绕过 Loaded SKILL.md 自由回答用户请求。\n"
                            "你不得自行编造业务结果、执行结果、计划内容、文件内容或命令输出。\n\n"
                            "如果 Loaded SKILL.md 语义上要求通过某种动作完成任务，"
                            "例如运行程序、调用脚本、执行命令、写入文件、读取资源、生成配置、运行测试或调用工具，"
                            "你必须先按照 Loaded SKILL.md 的原始要求输出该动作的实际形式。\n"
                            "动作表达形式由 Loaded SKILL.md 决定，不能固定假设某种章节、某种语言、某种命令或某种格式。\n\n"
                            "如果动作中包含示例输入、占位输入、演示参数或模板参数，"
                            "只要语义上对应当前用户输入，就必须替换为当前用户的真实输入。\n"
                            "不能在应替换时原样保留示例值或占位值。\n\n"
                            "如果缺少必要参数，必须明确指出缺少哪些信息；"
                            "不得猜测，不得保留占位符继续输出，不得直接编造最终结果。\n\n"
                            "只有当 Loaded SKILL.md 明确要求直接生成文本结果，"
                            "或者不存在任何外部动作要求时，才可以直接生成文本结果。\n"
                        ),
                    }
                )

            if response_route.task == "vision":
                final_messages.extend(_request_messages_with_inline_images(request, execution_root))
            else:
                final_messages.extend(_request_messages_with_files(request))

            assistant_chunks: list[str] = []
            ack_payload = {}

            def _capture_final_ack(payload: dict) -> None:
                ack_payload.update(payload)

            async for chunk in stream_chat(final_messages, response_model, model_ack_callback=_capture_final_ack):
                if ack_payload:
                    yield _sse({"model_ack": {**response_route.ack(actual_model=ack_payload.get("actual_model")), "provider": ack_payload}})
                    ack_payload.clear()
                assistant_chunks.append(chunk)
                if not enable_action_execution:
                    yield _sse({"content": chunk})

            assistant_text = "".join(assistant_chunks)

            if enable_action_execution:
                try:
                    exec_result = await _plan_and_execute_generated_output(
                        assistant_text=assistant_text,
                        request=request,
                        model=model,
                        require_confirmation=require_action_confirmation,
                        execution_root=execution_root,
                        skill_name=parent_skill_name,
                    )

                    if exec_result.get("executed"):
                        exec_result["assistant_draft"] = assistant_text

                        yield _sse({"status": {"phase": "generating", "message": "整合执行结果…"}})

                        final_answer = await _generate_final_answer_from_observation(
                            body_prompt=body_prompt,
                            request=request,
                            model=route_model(
                                TEXT_TASK,
                                requested_model=requested_model,
                                reason="sandbox finalization after actions",
                            ).model,
                            plan=locals().get("runtime_plan")
                            if isinstance(locals().get("runtime_plan"), dict)
                            else exec_result.get("plan", {}),
                            execution_result=exec_result,
                        )

                        output_files = exec_result.get("output_files") or []
                        final_answer = _finalize_answer_output_file_links(final_answer, output_files)
                        yield _sse({"content": final_answer})

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
                    logger.exception("legacy markdown action fallback failed")
                    yield _sse({"status": None})
                    yield _sse({"error": "错误：后台规划或执行文件操作失败"})
                    yield "data: [DONE]\n\n"
                    return
            if enable_action_execution and assistant_text and not locals().get("exec_result", {}).get("executed"):
                yield _sse({"content": assistant_text})
            yield _sse({"status": None})
            yield "data: [DONE]\n\n"

        except Exception as exc:
            logger.exception("LLM stream error")
            yield _sse({"status": None})
            yield _sse({"error": _friendly_error(exc)})
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
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
    try:
        skill_context = build_skill_context(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return _make_stream(skill_context, request)


class PlanConfirmRequest(BaseModel):
    """Request body for confirming a pending plan execution."""
    plan_id: str
    action: str = "confirm"  # "confirm" | "cancel"


@router.post("/sandbox/{skill_name}/confirm")
async def confirm_plan_execution(skill_name: str, request: PlanConfirmRequest):
    """Confirm or cancel a pending plan in Plan mode."""
    _cleanup_expired_plans()

    pending = _pending_plans.pop(request.plan_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="方案不存在或已过期，请重新发送请求。")

    if request.action == "cancel":
        return {"status": "cancelled", "message": "执行方案已取消。"}

    # Re-execute the plan by building a new stream with execute mode
    skill_context = pending["skill_context"]
    original_request = pending["request"]
    # Override to execute mode for actual execution
    original_request.execution_mode = "execute"

    return _make_stream(skill_context, original_request)


class SOPExportRequest(BaseModel):
    """Request body for SOP export."""
    plan_id: str | None = None
    format: str = "markdown"  # "markdown" | "json"


@router.post("/sandbox/{skill_name}/sop")
async def export_sop(skill_name: str, request: SOPExportRequest):
    """Export the SOP document for a pending or last-executed plan."""
    if request.plan_id:
        pending = _pending_plans.get(request.plan_id)
        if not pending:
            raise HTTPException(status_code=404, detail="方案不存在或已过期。")
        sop = pending.get("sop", {})
    else:
        raise HTTPException(status_code=400, detail="需要提供 plan_id。")

    if request.format == "json":
        return sop

    # Markdown format
    lines = [f"# {sop.get('title', 'SOP')}\n"]
    lines.append(f"**版本**：{sop.get('version', '1.0')}\n")
    lines.append(f"**技能**：{sop.get('skill_name', skill_name)}\n")
    lines.append(f"**复杂度**：{sop.get('complexity', '')}\n")
    lines.append(f"\n## 执行步骤\n")

    for step in sop.get("steps", []):
        lines.append(f"### 步骤 {step['order']}：{step['name']}\n")
        lines.append(f"- **描述**：{step.get('description', '')}\n")
        if step.get("inputs"):
            lines.append(f"- **输入**：{', '.join(step['inputs'])}\n")
        if step.get("outputs"):
            lines.append(f"- **输出**：{', '.join(step['outputs'])}\n")
        lines.append(f"- **执行者**：{step.get('responsible', 'agent')}\n")
        lines.append("")

    if sop.get("flowchart_mermaid"):
        lines.append("\n## 流程图\n")
        lines.append(f"```mermaid\n{sop['flowchart_mermaid']}\n```\n")

    return {"format": "markdown", "content": "\n".join(lines)}
