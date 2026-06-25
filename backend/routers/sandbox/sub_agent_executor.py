"""SubAgent 执行器：在独立沙箱上下文中执行特定领域任务。

每个 SubAgent 拥有独立的 LLM 对话历史、精简的 SKILL.md 段落、
本领域工具 Snippet，以及独立的权限策略。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time as _time
from pathlib import Path
from typing import Any

from ...config import settings
from ...services.llm_proxy import complete_chat_once, stream_chat
from ...services.model_router import route_model, TEXT_TASK, CODE_TASK
from ..chat_utils import (
    _last_user_text,
    _planner_model_name,
    _request_messages_with_files,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest
from .sub_agent_types import SubAgentPolicy, SubAgentResult, SubAgentTask, SubAgentType
from .sub_agent_policy import get_default_policy, describe_policy
from .skill_section_splitter import (
    filter_sections_for_sub_agent,
    split_skill_md_by_sections,
)
from .task_executor import _execute_single_task
from .path_resolution import _infer_skill_root_from_tasks
from .error_correction import (
    _get_llm_error_correction,
    _apply_error_correction,
)

logger = logging.getLogger(__name__)

# SubAgent 任务级重试次数（仅对 run_command 失败时触发 LLM 修正重试）
_MAX_SUB_AGENT_RETRY = 2


class SubAgentExecutor:
    """SubAgent 执行器：在独立沙箱上下文中执行特定领域任务。"""

    def __init__(
        self,
        sub_agent_type: SubAgentType,
        policy: SubAgentPolicy | None = None,
    ):
        self.sub_agent_type = sub_agent_type
        self.policy = policy or get_default_policy(sub_agent_type)
        self.messages: list[dict[str, str]] = []

    async def execute(
        self,
        task: SubAgentTask,
        skill_context: dict,
        request: ChatRequest,
        *,
        execution_root: Path | None = None,
        prior_results: list[SubAgentResult] | None = None,
        yield_func=None,
    ) -> SubAgentResult:
        """执行单个 SubAgent 任务。

        Args:
            task: Master 派发的任务。
            skill_context: 包含 body_loader 等的 skill 上下文字典。
            request: 用户请求。
            execution_root: Skill 执行根目录。
            prior_results: 前置依赖的 SubAgent 执行结果。
            yield_func: SSE 事件回调（可选）。

        Returns:
            SubAgentResult 执行结果。
        """
        start_ms = int(_time.time() * 1000)

        try:
            # 1. 构建精简上下文
            self.messages = self._build_focused_context(
                task, skill_context, request, prior_results=prior_results
            )

            # 2. 发送 SubAgent 启动事件
            if yield_func:
                yield_func(_sub_agent_event(
                    "sub_agent_start",
                    sub_agent_type=self.sub_agent_type.value,
                    task_id=task.task_id,
                    description=task.task_description[:200],
                ))

            # 3. 调用 LLM 生成执行方案
            model = self._route_model()
            planner_model = _planner_model_name(model)

            logger.info("[LLM_CALL] 阶段=sub_agent_execute 类型=%s 模型=%s 消息数=%d", self.sub_agent_type.value, planner_model, len(self.messages))
            logger.debug("[LLM_CALL] 阶段=sub_agent_execute 完整消息=%s", json.dumps(self.messages, ensure_ascii=False)[:2000])
            llm_response = await complete_chat_once(self.messages, planner_model)

            # 4. 解析 LLM 输出为可执行 tasks + 缺失资源信息
            parsed = self._parse_llm_response(llm_response)
            executable_tasks = parsed["tasks"]
            parsed_missing = parsed["missing"]
            parsed_need_ask_user = parsed["need_ask_user"]
            parsed_ask_user_message = parsed["ask_user_message"]

            # 5. 如果需要向用户询问信息，直接返回 ask_user 结果
            if parsed_need_ask_user:
                return SubAgentResult(
                    sub_agent_type=self.sub_agent_type,
                    task_id=task.task_id,
                    success=False,
                    output="",
                    output_files=[],
                    error="需要用户提供信息",
                    observations=[],
                    execution_time_ms=int(_time.time() * 1000) - start_ms,
                    missing=parsed_missing,
                    need_ask_user=True,
                    ask_user_message=parsed_ask_user_message,
                )

            # 6. 如果没有可执行任务，尝试直接用 LLM 流式回答
            if not executable_tasks:
                return SubAgentResult(
                    sub_agent_type=self.sub_agent_type,
                    task_id=task.task_id,
                    success=True,
                    output=llm_response[:2000],
                    output_files=[],
                    error=None,
                    observations=[{"type": "direct_answer", "content": llm_response[:1000]}],
                    execution_time_ms=int(_time.time() * 1000) - start_ms,
                )

            # 6. 逐任务执行
            all_results: list[dict] = []
            all_output_files: list[dict] = []
            all_touched: list[Path] = []
            accumulated_output_files: list[dict] = []

            # 提取 session_input_dir，与单 Agent 流程（_execute_planned_actions）保持一致
            from ..chat_utils import _extract_input_session_dir
            _session_input_dir = _extract_input_session_dir(
                getattr(request, "input_files", []) or [], execution_root
            )

            for exec_task in executable_tasks:
                # 沙盒流程中 execution_root 已是权威 Skill 根目录，无需推断。
                # _infer_skill_root_from_tasks 仅用于 Creator 流程（execution_root 未设置时）。
                inferred_root: Path | None = None
                if execution_root is None:
                    inferred_root = _infer_skill_root_from_tasks(
                        {"tasks": executable_tasks},
                        execution_root=execution_root,
                    )

                current_task = exec_task
                result: dict = {}
                task_touched: list[Path] = []

                # 任务级重试循环：对失败的 run_command 任务调用 LLM 修正后重试
                for retry_attempt in range(_MAX_SUB_AGENT_RETRY + 1):
                    try:
                        result, task_touched = await asyncio.to_thread(
                            functools.partial(
                                _execute_single_task,
                                current_task,
                                [],
                                request,
                                execution_root=execution_root,
                                inferred_skill_root=inferred_root,
                                skill_name=skill_context.get("skill_name", ""),
                                session_input_dir=_session_input_dir,
                                sandbox_policy=self.policy,
                                previous_output_files=accumulated_output_files or None,
                            )
                        )

                        # 成功或已达最大重试次数，退出重试循环
                        if result.get("success", True) or retry_attempt >= _MAX_SUB_AGENT_RETRY:
                            break

                        # 仅对 run_command 任务进行 LLM 修正重试
                        if current_task.get("action") != "run_command":
                            break

                        logger.info(
                            "[SUB_AGENT_RETRY] sub_agent=%s task_action=run_command "
                            "attempt=%d/%d failed, requesting LLM correction",
                            self.sub_agent_type.value, retry_attempt + 1, _MAX_SUB_AGENT_RETRY,
                        )

                        # 调用 LLM 分析错误并生成修正建议
                        correction = await _get_llm_error_correction(
                            task=current_task,
                            error_result=result,
                            attempt=retry_attempt + 1,
                            max_retries=_MAX_SUB_AGENT_RETRY,
                            body_prompt=skill_context.get("body_prompt", "") or "",
                            model=self._route_model(),
                        )

                        if not correction.get("corrected"):
                            logger.info(
                                "[SUB_AGENT_RETRY] LLM 无法提供修正建议: %s",
                                correction.get("reason", ""),
                            )
                            break

                        # 应用修正并重试
                        current_task = _apply_error_correction(current_task, correction)
                        logger.info(
                            "[SUB_AGENT_RETRY] LLM 已修正任务，重试执行: %s",
                            correction.get("reason", ""),
                        )

                    except PermissionError as exc:
                        logger.warning(
                            "SubAgent %s policy violation: %s",
                            self.sub_agent_type.value, exc,
                        )
                        result = {
                            "action": exec_task.get("action", ""),
                            "success": False,
                            "error": str(exc),
                        }
                        break  # 权限错误不重试

                    except Exception as exc:
                        logger.warning(
                            "SubAgent %s task execution failed: %s",
                            self.sub_agent_type.value, exc,
                        )
                        result = {
                            "action": exec_task.get("action", ""),
                            "success": False,
                            "error": str(exc),
                        }
                        # 异常错误不重试（非命令执行失败）
                        break

                all_results.append(result)
                all_touched.extend(task_touched)
                if result.get("output_files"):
                    accumulated_output_files.extend(result["output_files"])
                    all_output_files.extend(result["output_files"])

                if yield_func:
                    yield_func(_sub_agent_event(
                        "sub_agent_task_result",
                        sub_agent_type=self.sub_agent_type.value,
                        task_id=task.task_id,
                        action=exec_task.get("action", ""),
                        success=result.get("success", True),
                    ))

            # 7. 构建结果
            # 成功判定：如果有产出文件且有至少一个成功任务，标记为成功（部分成功）。
            # 这避免因单个非关键任务失败而触发 Master 不必要的重规划。
            success_count = sum(1 for r in all_results if r.get("success", False))
            total_count = len(all_results)
            if total_count == 0:
                success = False
            elif success_count == total_count:
                success = True
            else:
                # 部分成功：如果有产出文件且至少有一个成功任务，标记为成功
                success = bool(all_output_files) and success_count > 0

            # 执行完整性检查：检测"只创建配置文件未执行脚本"的情况
            # 如果所有任务都是 write_file/create_directory，没有 run_command，
            # 且任务描述中包含执行类关键词（查询/生成/执行/导出），则降级为失败
            # 这防止 SubAgent 仅创建配置文件就标记成功，误导 Master 认为任务完成
            has_run_command = any(r.get("action") == "run_command" for r in all_results)
            has_only_file_ops = all(
                r.get("action") in ("write_file", "create_directory")
                for r in all_results
            ) if all_results else False
            task_desc_lower = (task.task_description or "").lower()
            execution_keywords = ["查询", "生成", "执行", "导出", "统计", "query", "generate", "execute", "export"]
            needs_execution = any(kw in task_desc_lower for kw in execution_keywords)

            if success and has_only_file_ops and not has_run_command and needs_execution:
                logger.warning(
                    "[SUB_AGENT_INCOMPLETE] sub_agent=%s task_id=%s only created files without executing script, "
                    "marking as failed. tasks=%d, output_files=%d",
                    self.sub_agent_type.value, task.task_id, total_count, len(all_output_files),
                )
                success = False

            output_parts = []
            raw_stdout_parts = []
            for r in all_results:
                action = r.get("action", "")
                if r.get("success"):
                    if action == "run_command":
                        stdout = (r.get("stdout") or "")[:500]
                        output_parts.append(f"[{action}] 成功: {stdout}")
                        raw_stdout_parts.append(r.get("stdout") or "")
                    elif action == "read_resource":
                        content_len = len(r.get("content", ""))
                        output_parts.append(f"[{action}] 成功: {content_len}字符")
                    elif action == "write_file":
                        path = r.get("path", "")
                        output_parts.append(f"[{action}] 成功: {path}")
                    else:
                        output_parts.append(f"[{action}] 成功")
                else:
                    err = r.get("error") or r.get("stderr") or "未知错误"
                    output_parts.append(f"[{action}] 失败: {err[:200]}")

            logger.info(
                "[OUTPUT_FILES] sub_agent=%s task_id=%s success=%s output_files_count=%d",
                self.sub_agent_type.value,
                task.task_id,
                success,
                len(all_output_files),
            )

            result = SubAgentResult(
                sub_agent_type=self.sub_agent_type,
                task_id=task.task_id,
                success=success,
                output="\n".join(output_parts),
                output_files=all_output_files,
                error=None if success else output_parts[-1] if output_parts else "执行失败",
                observations=[{"type": "execution_result", "summary": output_part} for output_part in output_parts],
                execution_time_ms=int(_time.time() * 1000) - start_ms,
                missing=parsed_missing if parsed_missing else self._extract_missing_from_errors(all_results),
                raw_stdout="\n".join(raw_stdout_parts) if raw_stdout_parts else "",
                error_type="" if success else self._analyze_error_type(all_results),
                error_detail={} if success else self._analyze_error_detail(all_results),
            )

            if yield_func:
                yield_func(_sub_agent_event(
                    "sub_agent_complete",
                    sub_agent_type=self.sub_agent_type.value,
                    task_id=task.task_id,
                    success=result.success,
                ))

            return result

        except Exception as exc:
            logger.exception("SubAgent %s execution error: %s", self.sub_agent_type.value, exc)
            return SubAgentResult(
                sub_agent_type=self.sub_agent_type,
                task_id=task.task_id,
                success=False,
                output="",
                error=str(exc),
                execution_time_ms=int(_time.time() * 1000) - start_ms,
            )

    def _build_focused_context(
        self,
        task: SubAgentTask,
        skill_context: dict,
        request: ChatRequest,
        *,
        prior_results: list[SubAgentResult] | None = None,
    ) -> list[dict[str, str]]:
        """构建 SubAgent 的精简 LLM 上下文。"""
        messages: list[dict[str, str]] = []

        # 1. 系统提示：SubAgent 角色定义 + 权限约束
        messages.append({
            "role": "system",
            "content": self._compose_sub_agent_system_prompt(),
        })

        # 2. SKILL.md 片段：仅加载本领域相关段落
        # 优先使用 skill_context 中已缓存的 body_prompt，避免重复调用 body_loader
        full_body = skill_context.get("body_prompt")
        if not full_body:
            body_loader = skill_context.get("body_loader")
            if body_loader:
                full_body = body_loader()
        if full_body:
            sections = split_skill_md_by_sections(full_body)
            focused_body = filter_sections_for_sub_agent(sections, self.sub_agent_type)

            # 上下文充足性检测：如果过滤后内容过短（少于 500 字符），
            # 说明领域过滤过于激进，回退到完整 SKILL.md 确保 SubAgent 有足够上下文执行任务
            if not focused_body or len(focused_body) < 500:
                logger.info(
                    "[SUB_AGENT_CONTEXT] sub_agent=%s filtered_body too short (%d chars), "
                    "falling back to full SKILL.md (%d chars) to ensure sufficient context",
                    self.sub_agent_type.value, len(focused_body or ""), len(full_body),
                )
                focused_body = full_body

            if focused_body:
                messages.append({
                    "role": "system",
                    "content": f"## Loaded SKILL.md（{self.sub_agent_type.value}领域相关段落）\n\n{focused_body}",
                })

        # 3. 工具 Snippet：仅注入本领域允许的工具
        focused_tools = self._compose_focused_tools_prompt()
        if focused_tools:
            messages.append({
                "role": "system",
                "content": focused_tools,
            })

        # 4. 前置依赖结果（结构化数据 + 摘要）
        if prior_results:
            structured_parts = []
            for r in prior_results:
                if not r.success:
                    continue
                section = f"### [{r.sub_agent_type.value}] task={r.task_id}\n"
                # 摘要
                section += f"摘要: {r.output[:500]}\n"
                # 原始输出（关键结构化数据，如数据库查询结果）
                if r.raw_stdout:
                    section += f"原始输出:\n```\n{r.raw_stdout[:2000]}\n```\n"
                # 产出文件路径
                if r.output_files:
                    file_lines = []
                    for f in r.output_files[:10]:
                        rel_path = f.get('path', '')
                        file_lines.append(f"  - {rel_path}")
                    section += "产出文件（相对于 Skill 根目录，可在命令中直接使用）:\n"
                    section += "\n".join(file_lines) + "\n"
                structured_parts.append(section)

            if structured_parts:
                messages.append({
                    "role": "system",
                    "content": "## 前置执行结果\n\n" + "\n\n---\n\n".join(structured_parts),
                })

        # 5. 任务描述 + 用户请求
        user_text = _last_user_text(request)
        messages.append({
            "role": "user",
            "content": f"任务：{task.task_description}\n\n用户请求：{user_text}",
        })

        return messages

    def _compose_sub_agent_system_prompt(self) -> str:
        """SubAgent 角色定义 prompt。"""
        type_descriptions = {
            SubAgentType.DATA_QUERY: "数据库查询、数据统计、数据导出",
            SubAgentType.CODE_SCRIPT: "脚本执行、文件操作、命令行工具",
            SubAgentType.NETWORK_API: "HTTP请求、API调用、网络爬虫",
            SubAgentType.DOCUMENT: "文档解析、格式转换、内容提取、报告生成",
            SubAgentType.VALIDATION: "输出校验、安全审查",
        }
        desc = type_descriptions.get(self.sub_agent_type, "通用执行")
        policy_desc = describe_policy(self.policy)

        available_actions = ["read_resource"]
        if self.policy.allow_subprocess:
            available_actions.append("run_command")
        if self.policy.allow_file_write:
            available_actions.extend(["write_file", "create_directory"])
        actions_str = " | ".join(available_actions)

        field_lines = []
        if "run_command" in available_actions:
            field_lines.append('      "command": "要执行的命令（run_command时）",')
        if "write_file" in available_actions or "create_directory" in available_actions:
            field_lines.append('      "path": "文件路径（write_file/read_resource/create_directory时）",')
            field_lines.append('      "content": "文件内容（write_file时）",')
        else:
            field_lines.append('      "path": "文件路径（read_resource时）",')
        field_lines.append('      "reason": "为什么需要该动作"')
        fields_str = "\n".join(field_lines)

        return (
            f"你是 {self.sub_agent_type.value} 领域的专业执行 Agent。\n"
            f"你的专业范围：{desc}\n"
            f"你的权限边界：{policy_desc}\n"
            f"你可以使用的 action：{actions_str}\n\n"
            "严格遵循 SKILL.md 中的执行规范，只使用允许的工具和能力。\n"
            "不要尝试输出你权限范围外的 action，否则会被拒绝执行。\n\n"
            "重要规则：\n"
            "1. 执行脚本前，先检查脚本依赖的配置文件是否存在。如果不存在且你有 write_file 权限，"
            "先用 write_file 创建配置文件，再执行脚本。\n"
            "2. 如果缺少只有用户才知道的信息（如数据库连接参数、API密钥等），"
            "设置 need_ask_user=true 并在 ask_user_message 中说明需要什么信息。"
            "不要编造或猜测用户私有信息。\n"
            "3. 如果缺少的是模型可以创建的资源（如配置文件、目录），在 tasks 中先用 write_file/create_directory 创建，"
            "不要设置 need_ask_user。\n"
            "4. 执行类任务完整性要求：如果你的任务涉及执行脚本（如数据库查询、文档生成、数据处理），\n"
            "   必须在 tasks 中同时包含：\n"
            "   a) write_file 创建所需配置文件（如果配置不存在）\n"
            "   b) run_command 执行脚本完成实际任务\n"
            "   绝对不要只创建配置文件就停止——必须执行脚本完成用户请求的实际目标。\n"
            "5. 配置文件路径必须遵循 SKILL.md 中的约定（如 config/db_config.json），\n"
            "   不要自行更改路径（如改为 assets/generated/db_config.json）。\n"
            "6. 执行脚本时，命令中的脚本路径必须使用 scripts/xxx.py 格式\n"
            "   （相对于 Skill 根目录），不要使用 skills/<name>/scripts/xxx.py 全路径\n"
            "   或裸文件名。例如：\n"
            "   正确：python scripts/db_query.py \"SELECT * FROM table_name\"\n"
            "   错误：python skills/database-query/scripts/db_query.py \"...\"\n"
            "7. run_command 命令不支持 shell 管道、重定向、命令替换等特性，\n"
            "   因为平台以非交互模式（shell=False）逐条执行命令。禁止使用 |、>、>>、<、&&、||、$()、`` 等 shell 语法。\n"
            "   如需将脚本输出保存到文件，应在脚本内部实现文件写入，\n"
            "   或在 tasks 中先 run_command 执行脚本，再用 write_file 保存结果。\n"
            "   正确：python scripts/db_query.py \"SELECT * FROM table_name\"\n"
            "   错误：python scripts/db_query.py \"SELECT * FROM table_name\" | tee -a report.txt\n"
            "   错误：python scripts/db_query.py \"SELECT * FROM table_name\" > output.json\n\n"
            "文件输出约定：\n"
            "1. 生成的文件应放在 assets/generated/ 或 outputs/ 目录下，使用相对于 Skill 根目录的相对路径。\n"
            "2. 脚本 stdout 应输出 JSON，包含产出文件路径字段：\n"
            "   - PDF: pdf_path 或 file_paths\n"
            "   - 图片: image_path 或 image_paths\n"
            "   - 通用: file_path 或 file_paths\n"
            "3. 路径格式示例：assets/generated/fairy_tale.pdf（不要用绝对路径）。\n"
            "4. 如果前置任务产出了文件，这些文件路径会在「前置执行结果」中给出，可直接在命令参数中使用。\n"
            "5. 执行脚本时，把前置产出文件路径作为命令参数传入（如 image_paths），不要自行猜测路径。\n\n"
            "输出格式（严格 JSON）：\n"
            "{\n"
            '  "tasks": [\n'
            "    {\n"
            f'      "action": "{actions_str}",\n'
            f"{fields_str}\n"
            "    }\n"
            "  ],\n"
            '  "missing": [{"type": "config_file|user_info", "path": "缺失资源路径", "reason": "为什么需要"}],\n'
            '  "need_ask_user": false,\n'
            '  "ask_user_message": ""\n'
            "}\n\n"
            "只输出 JSON，不要输出其他内容。\n"
        )

    def _compose_focused_tools_prompt(self) -> str:
        """仅注入本 SubAgent 允许的工具 Snippet。"""
        try:
            from ...services.creator_tool_registry import (
                list_tool_capabilities,
                snippets_for_tool,
                format_tool_snippet,
            )
        except ImportError:
            return ""

        capabilities = list_tool_capabilities()
        filtered = [
            cap for cap in capabilities
            if cap.enabled_by_default
            and (
                cap.category in self.policy.allowed_tool_categories
                or any(
                    role in self.policy.allowed_tool_roles
                    for role in (cap.roles or [])
                )
            )
        ]

        if not filtered:
            return ""

        snippet_texts = []
        for cap in filtered:
            for snippet in snippets_for_tool(cap):
                snippet_texts.append(format_tool_snippet(cap, snippet))

        if not snippet_texts:
            return ""

        snippets_block = "\n\n---\n\n".join(snippet_texts)
        return (
            "## 平台内置工具能力（仅本领域）\n\n"
            "你可以使用以下工具完成任务：\n\n"
            f"{snippets_block}\n"
        )

    def _route_model(self) -> str:
        """根据 SubAgent 类型路由模型。"""
        if self.sub_agent_type == SubAgentType.CODE_SCRIPT:
            return route_model(CODE_TASK, reason="sub_agent code_script").model
        return route_model(TEXT_TASK, reason=f"sub_agent {self.sub_agent_type.value}").model

    def _parse_llm_response(self, llm_response: str) -> dict:
        """解析 LLM 输出为可执行 tasks 列表 + 缺失资源信息。

        Returns:
            {"tasks": [...], "missing": [...], "need_ask_user": bool, "ask_user_message": str}
        """
        try:
            stripped = _strip_markdown_json_fence(llm_response)
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            # 如果不是合法 JSON，尝试提取 fenced code blocks 作为命令
            tasks = self._extract_tasks_from_text(llm_response)
            return {"tasks": tasks, "missing": [], "need_ask_user": False, "ask_user_message": ""}

        if not isinstance(parsed, dict):
            return {"tasks": [], "missing": [], "need_ask_user": False, "ask_user_message": ""}

        tasks = parsed.get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []

        # 过滤只保留允许的 action 类型
        allowed_actions = {"run_command", "write_file", "read_resource", "create_directory"}
        filtered_tasks = [t for t in tasks if isinstance(t, dict) and t.get("action") in allowed_actions]

        # 解析缺失资源和 ask_user 信息
        missing = parsed.get("missing") or []
        if not isinstance(missing, list):
            missing = []
        need_ask_user = bool(parsed.get("need_ask_user", False))
        ask_user_message = str(parsed.get("ask_user_message") or "").strip()

        return {
            "tasks": filtered_tasks,
            "missing": [m for m in missing if isinstance(m, dict)],
            "need_ask_user": need_ask_user,
            "ask_user_message": ask_user_message,
        }

    def _extract_tasks_from_text(self, text: str) -> list[dict]:
        """从非 JSON 文本中提取可执行任务（fenced code blocks）。"""
        import re
        tasks = []

        # 提取 ```bash / ```python fenced blocks
        fence_pattern = re.compile(
            r"```(?:bash|python|sh|shell|javascript|js)?\s*\n(.*?)```",
            re.DOTALL,
        )
        for m in fence_pattern.finditer(text):
            code = m.group(1).strip()
            if code:
                tasks.append({
                    "action": "run_command",
                    "command": code,
                    "reason": "从 LLM 输出中提取的代码块",
                })

        return tasks

    # ------------------------------------------------------------------
    # 错误分析方法
    # ------------------------------------------------------------------

    @staticmethod
    def _analyze_error_type(all_results: list[dict]) -> str:
        """根据执行结果推断错误类型。"""
        for r in all_results:
            if r.get("success"):
                continue
            error_str = str(r.get("error") or r.get("stderr") or "").lower()
            action = r.get("action", "")
            # 文件/资源缺失
            if "filenotfounderror" in error_str or "no such file" in error_str or "not found" in error_str:
                return "resource_missing"
            # 配置文件缺失（脚本运行时报告）
            if "config" in error_str and ("not found" in error_str or "missing" in error_str or "no such" in error_str):
                return "resource_missing"
            # 权限拒绝
            if "permissionerror" in error_str or "permission denied" in error_str or "access denied" in error_str:
                return "permission_denied"
            # 超时
            if "timeout" in error_str or "timed out" in error_str:
                return "timeout"
            # 连接错误
            if "connectionerror" in error_str or "connection refused" in error_str or "could not connect" in error_str:
                return "resource_missing"
        return "execution_failed"

    @staticmethod
    def _analyze_error_detail(all_results: list[dict]) -> dict:
        """从执行结果中提取结构化错误详情。"""
        for r in all_results:
            if r.get("success"):
                continue
            detail: dict = {}
            action = r.get("action", "")
            if action:
                detail["action"] = action
            if r.get("command"):
                detail["command"] = str(r["command"])[:200]
            if r.get("path"):
                detail["path"] = str(r["path"])
            error_str = str(r.get("error") or r.get("stderr") or "")
            if error_str:
                detail["error_message"] = error_str[:500]
            return detail
        return {}

    @staticmethod
    def _extract_missing_from_errors(all_results: list[dict]) -> list[dict]:
        """从错误信息中提取缺失的资源路径，生成 missing 列表。"""
        missing: list[dict] = []
        seen_paths: set[str] = set()
        import re as _re
        # 匹配文件路径模式
        path_pattern = _re.compile(r'(?:No such file|not found|cannot find|cannot open|FileNotFoundError)[^\n]*?[\s"\']([\w/\\]+\.\w+)', _re.IGNORECASE)
        for r in all_results:
            if r.get("success"):
                continue
            error_str = str(r.get("error") or r.get("stderr") or "")
            action = r.get("action", "")
            # 从 write_file / create_directory 的 path 字段提取
            if action in {"write_file", "create_directory"} and r.get("path"):
                path = str(r["path"])
                if path not in seen_paths:
                    seen_paths.add(path)
                    missing.append({"type": "resource", "path": path, "reason": f"{action} 操作失败"})
            # 从 run_command 的错误信息中提取缺失文件路径
            for match in path_pattern.finditer(error_str):
                path = match.group(1).replace("\\", "/")
                if path not in seen_paths:
                    seen_paths.add(path)
                    missing.append({"type": "resource", "path": path, "reason": "执行中报告文件缺失"})
            # 从 run_command 的 command 字段提取脚本路径
            if action == "run_command" and r.get("command"):
                cmd = str(r["command"])
                # 提取 python scripts/xxx.py 中的脚本路径
                script_match = _re.search(r'python\s+(scripts/\S+\.py)', cmd)
                if script_match and "not found" in error_str.lower():
                    path = script_match.group(1)
                    if path not in seen_paths:
                        seen_paths.add(path)
                        missing.append({"type": "script", "path": path, "reason": "脚本执行失败"})
        return missing


def _sub_agent_event(event_type: str, **data: Any) -> str:
    """构建 SubAgent SSE 事件。"""
    from ..chat_utils import _sse
    return _sse({"type": event_type, **data})