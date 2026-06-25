"""Master Agent：任务拆解、SubAgent 调度、结果汇总。

Master Agent 是多智能体架构的主控层，负责：
1. 分析用户请求，拆解为 SubAgent 任务列表
2. 按依赖拓扑排序，调度 SubAgent 执行
3. 汇总所有 SubAgent 结果，生成最终回答

Master 自身不执行任何业务 Skill，仅做规划与调度。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid
from pathlib import Path
from typing import Any

from ...config import settings
from ...services.llm_proxy import complete_chat_once
from ...services.model_router import route_model, TEXT_TASK, PLANNER_TASK
from ..chat_utils import (
    _last_user_text,
    _planner_model_name,
    _request_messages_with_files,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest
from .sub_agent_types import (
    MasterPlan,
    SubAgentResult,
    SubAgentTask,
    SubAgentType,
)
from .sub_agent_executor import SubAgentExecutor
from .skill_section_splitter import (
    get_section_summary,
    split_skill_md_by_sections,
)
from .final_answer import _generate_final_answer_from_observation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Master 调度 prompt
# ---------------------------------------------------------------------------

_MASTER_DECOMPOSE_PROMPT = """\
你是 Skill 执行调度器。根据用户请求和 Skill 结构，将任务拆解为专业执行 Agent 参与。

可用 Agent 类型及能力边界：
- data_query：数据库查询、数据统计、数据导出。可执行数据库相关脚本，可写入配置文件，禁止网络访问和数据库写操作。
- code_script：通用脚本执行、文件操作、命令行工具。可执行任意脚本、可写入文件，禁止网络访问。
- network_api：HTTP 请求、API 调用、网络爬虫。可访问网络，禁止写文件和执行脚本。
- document：文档解析、格式转换、内容提取、报告生成。可执行文档生成脚本、可写入文件，禁止网络访问。
- validation：输出校验、安全审查。禁止所有写操作和网络访问。

核心原则：
1. 只拆解真正需要不同专业能力的任务
2. 如果用户请求只需一种 Agent 类型，只输出一个任务
3. 任务间如果有依赖（如数据查询结果需要传给文档导出），在 depends_on 中标注对应的 task_id
4. 每个 task 的 task_description 必须清晰、具体、可执行
5. skill_sections 列出该任务需要参考的 SKILL.md 段落标题
6. 如果 Skill 的 SKILL.md 中声明了 scripts/ 下的脚本命令，应将执行该脚本的任务分配给对应领域的 Agent（数据库脚本→data_query，文档生成脚本→document，其他→code_script）
7. 如果用户请求中缺少必要信息（如数据库连接参数），且 SKILL.md 中明确要求这些参数，设置 need_ask_user=true 并在 missing_info 中列出缺少的信息
8. 不要编造或猜测用户的数据库连接参数、API密钥等私有信息

注意：不要在拆解阶段预测缺失资源或配置文件，资源检测由执行 Agent 在运行时自然发现并反馈。

文件路径约定：
1. 所有生成文件应放在 assets/generated/ 或 outputs/ 目录下
2. 任务描述中应说明预期产出文件的路径格式（如 assets/generated/fairy_tale.pdf）
3. 后续任务可通过前置任务的产出文件路径引用这些文件
4. 脚本 stdout 应输出 JSON，包含产出文件路径字段（pdf_path、image_paths、file_paths 等）

输出格式（严格 JSON，不要 Markdown）：
{
  "sub_agent_tasks": [
    {
      "task_id": "task_1",
      "sub_agent_type": "data_query | code_script | network_api | document | validation",
      "task_description": "具体的任务描述",
      "skill_sections": ["相关段落标题1", "相关段落标题2"],
      "resources": ["resource:0"],
      "depends_on": []
    }
  ],
  "need_ask_user": false,
  "missing_info": ["需要向用户询问的信息列表"]
}

task_id 规则：使用简单的唯一标识符（如 task_1、task_2、task_3），用于在 depends_on 中引用其他任务。
"""


class MasterAgent:
    """Master 主控 Agent：任务拆解、SubAgent 调度、结果汇总。"""

    async def analyze_and_decompose(
        self,
        metadata_prompt: str,
        body_prompt: str,
        request: ChatRequest,
        model: str,
        *,
        execution_root: Path | None = None,
    ) -> MasterPlan:
        """分析用户请求，拆解为 SubAgent 任务列表。

        Args:
            metadata_prompt: Skill 的 metadata 提示词。
            body_prompt: Skill 的完整正文（仅用于分析结构，不全量注入 SubAgent）。
            request: 用户请求。
            model: 默认模型。
            execution_root: Skill 执行根目录。

        Returns:
            MasterPlan 包含拆解后的任务列表和执行顺序。
        """
        # 1. 分析 SKILL.md 结构
        sections = split_skill_md_by_sections(body_prompt)
        section_summary = get_section_summary(sections)

        # 2. 构建 Master 的分析上下文（轻量，不全量注入）
        user_text = _last_user_text(request)

        # 预先计算智能 fallback 类型（用于 LLM 失败时）
        fallback_type = self._infer_fallback_agent_type(metadata_prompt, body_prompt)

        messages = [
            {"role": "system", "content": _MASTER_DECOMPOSE_PROMPT},
            {"role": "system", "content": (
                "## Skill 元数据\n\n"
                f"{metadata_prompt[:2000]}\n\n"
                "## SKILL.md 段落结构\n\n"
                f"{json.dumps(section_summary, ensure_ascii=False, indent=2)}"
            )},
            {"role": "user", "content": f"用户请求：{user_text}"},
            {"role": "user", "content": "请根据以上信息，输出 JSON 格式的任务拆解。只输出 JSON，不要其他内容。"},
        ]

        # 3. 调用 LLM 进行任务拆解
        planner_model = _planner_model_name(model)
        try:
            logger.info("[LLM_CALL] 阶段=master_decompose 模型=%s 消息数=%d", planner_model, len(messages))
            logger.debug("[LLM_CALL] 阶段=master_decompose 完整消息=%s", json.dumps(messages, ensure_ascii=False)[:2000])
            response_text = await complete_chat_once(messages, planner_model)
        except Exception as exc:
            logger.warning("Master decompose LLM call failed: %s, using intelligent fallback: %s", exc, fallback_type.value)
            return self._build_fallback_plan(user_text, fallback_type)

        # 4. 解析 JSON + 架构级校验
        try:
            stripped = _strip_markdown_json_fence(response_text)
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Master decompose response is not valid JSON, using intelligent fallback: %s. response=%s",
                fallback_type.value, response_text[:300],
            )
            return self._build_fallback_plan(user_text, fallback_type)

        # 架构级校验：检测错误阶段响应 + schema 校验
        is_valid, invalid_reason = self._validate_decompose_response(parsed)
        if not is_valid:
            logger.warning(
                "Master decompose response invalid: %s, using intelligent fallback: %s. response=%s",
                invalid_reason, fallback_type.value, response_text[:300],
            )
            return self._build_fallback_plan(user_text, fallback_type)

        # 5. 校验通过，正常解析任务
        parsed_tasks = self._parse_decompose_response(response_text)
        sub_agent_tasks = parsed_tasks["tasks"]
        need_ask_user = parsed_tasks["need_ask_user"]
        missing_info = parsed_tasks["missing_info"]

        # 如果需要向用户询问信息，返回 ask_user 模式的 MasterPlan
        if need_ask_user and missing_info:
            return MasterPlan(
                sub_agent_tasks=[],
                execution_order=[],
                mode="ask_user",
                need_ask_user=True,
                missing_info=missing_info,
            )

        # 校验通过后不应出现空任务，但保留防御性兜底
        if not sub_agent_tasks:
            logger.warning(
                "Master decompose produced empty tasks after validation, using intelligent fallback: %s",
                fallback_type.value,
            )
            return self._build_fallback_plan(user_text, fallback_type)

        # 6. 构建执行顺序（拓扑排序）
        execution_order = self._topological_sort(sub_agent_tasks)

        return MasterPlan(
            sub_agent_tasks=sub_agent_tasks,
            execution_order=execution_order,
            mode="multi_agent",
        )

    async def dispatch_and_execute(
        self,
        plan: MasterPlan,
        skill_context: dict,
        request: ChatRequest,
        model: str,
        *,
        execution_root: Path | None = None,
        yield_func=None,
        resume_from_index: int = 0,
        prior_results: list[SubAgentResult] | None = None,
    ) -> list[SubAgentResult]:
        """调度 SubAgent 执行任务，支持动态重规划和中断恢复。

        当前实现为串行调度（按拓扑排序顺序逐个执行）。
        当 SubAgent 报告缺失资源时，基于错误反馈动态决策
        （创建中间任务 / 询问用户 / 跳过）。

        Args:
            plan: Master 的执行计划。
            skill_context: Skill 上下文。
            request: 用户请求。
            model: 默认模型。
            execution_root: Skill 执行根目录。
            yield_func: SSE 事件回调。
            resume_from_index: 从第几个任务开始执行（用于中断恢复）。
            prior_results: 恢复前已完成的结果（用于构建 result_map）。

        Returns:
            所有 SubAgent 的执行结果列表。
        """
        results: list[SubAgentResult] = list(prior_results) if prior_results else []
        result_map: dict[str, SubAgentResult] = {r.task_id: r for r in results}

        ordered_tasks = list(plan.get_execution_order())
        _MAX_REPLAN_ROUNDS = 2

        for task_index, task in enumerate(ordered_tasks):
            # 跳过已执行的任务（恢复模式）
            if task_index < resume_from_index:
                continue

            # 收集前置依赖结果
            task_prior = [
                result_map[dep_id]
                for dep_id in task.depends_on
                if dep_id in result_map
            ]

            # 防御性兜底：如果 depends_on 没匹配到任何结果，
            # 把所有已完成的成功结果作为前置上下文传给当前 SubAgent。
            # 这确保后续 SubAgent 能看到前置 SubAgent 的产出文件路径，
            # 即使 LLM 输出的 task_id/depends_on 格式不规范也能工作。
            if not task_prior:
                all_prior_success = [r for r in results if r.success]
                if all_prior_success:
                    task_prior = all_prior_success
                    logger.info(
                        "[MASTER_DISPATCH] task=%s depends_on=%s 未匹配，"
                        "使用 %d 个已完成成功结果作为前置上下文兜底",
                        task.task_id, task.depends_on, len(task_prior),
                    )

            # 创建 SubAgent 执行器
            executor = SubAgentExecutor(task.sub_agent_type)

            # 执行
            if yield_func:
                yield_func(_master_event(
                    "master_dispatch",
                    task_id=task.task_id,
                    sub_agent_type=task.sub_agent_type.value,
                    description=task.task_description[:200],
                ))

            result = await executor.execute(
                task,
                skill_context,
                request,
                execution_root=execution_root,
                prior_results=task_prior if task_prior else None,
                yield_func=yield_func,
            )

            results.append(result)
            result_map[task.task_id] = result

            # 动态重规划：SubAgent 主动报告需要向用户询问
            if result.need_ask_user:
                return results

            # 动态重规划：基于错误反馈决策
            if not result.success and _MAX_REPLAN_ROUNDS > 0:
                decision = self._decide_from_error(result, task, results)

                if decision["action"] == "ask_user":
                    result.need_ask_user = True
                    result.ask_user_message = decision.get("ask_user_message") or "缺少必要信息，请补充"
                    return results

                elif decision["action"] == "create_intermediate":
                    _MAX_REPLAN_ROUNDS -= 1
                    pre_task = decision["intermediate_task"]

                    # 执行前置中间任务
                    pre_result = await self._execute_intermediate_task(
                        pre_task, skill_context, request, execution_root, yield_func,
                    )
                    results.append(pre_result)
                    result_map[pre_task.task_id] = pre_result

                    # 前置成功则重试原任务（传入前置结果）
                    if pre_result.success:
                        retry_result = await self._retry_with_prior(
                            task, skill_context, request, execution_root,
                            task_prior + [pre_result], yield_func,
                        )
                        # 替换原失败结果
                        results = [r for r in results if r.task_id != task.task_id]
                        results.append(retry_result)
                        result_map[task.task_id] = retry_result

                elif decision["action"] == "skip":
                    logger.info(
                        "Skipping task %s: %s", task.task_id, decision.get("reason", "")
                    )

        return results

    async def aggregate_and_answer(
        self,
        results: list[SubAgentResult],
        body_prompt: str,
        request: ChatRequest,
        model: str,
        *,
        execution_root: Path | None = None,
        skill_name: str = "",
    ) -> str:
        """汇总 SubAgent 结果，生成最终回答。

        仅注入结果摘要 + 必要的 SKILL.md 片段，不全量注入 body_prompt。

        Args:
            results: SubAgent 执行结果列表。
            body_prompt: SKILL.md 全文（仅用于 fallback）。
            request: 用户请求。
            model: 回答模型。
            execution_root: Skill 执行根目录。
            skill_name: Skill 名称。

        Returns:
            最终回答文本。
        """
        # 构建精简的执行结果摘要
        success_results = [r for r in results if r.success]
        failed_results = [r for r in results if not r.success]

        # 收集所有输出文件
        all_output_files: list[dict] = []
        for r in results:
            all_output_files.extend(r.output_files or [])

        # 构建 exec_result 兼容 _generate_final_answer_from_observation
        exec_result = {
            "executed": bool(results),
            "reason": "已通过 Master-SubAgent 多智能体架构执行。",
            "plan": {"mode": "multi_agent"},
            "results": [
                {
                    "action": f"sub_agent_{r.sub_agent_type.value}",
                    "task_id": r.task_id,
                    "sub_agent_type": r.sub_agent_type.value,
                    "success": r.success,
                    "stdout": r.raw_stdout[:5000] if r.raw_stdout else r.output[:2000],
                    "stderr": r.error or "",
                    "output_files": r.output_files,
                }
                for r in results
            ],
            "logs": [],
            "output_files": all_output_files,
        }

        try:
            final_answer = await _generate_final_answer_from_observation(
                body_prompt=body_prompt,
                request=request,
                model=model,
                plan=exec_result["plan"],
                execution_result=exec_result,
            )
        except Exception as exc:
            logger.warning("Master final answer generation failed: %s", exc)
            # Fallback: 直接拼接结果
            parts = []
            for r in results:
                status = "成功" if r.success else "失败"
                parts.append(f"[{r.sub_agent_type.value}] {status}: {r.output[:500]}")
            final_answer = "\n\n".join(parts)

        return final_answer

    # -----------------------------------------------------------------------
    #  内部方法
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    #  动态决策方法
    # -----------------------------------------------------------------------

    # 可由 SubAgent 自动创建的 missing 类型
    _CREATABLE_MISSING_TYPES = frozenset({"config_file", "script", "data_file", "resource"})
    # 仅用户知道的 missing 类型
    _USER_ONLY_MISSING_TYPES = frozenset({"user_info", "connection_param", "api_key", "credential"})
    # 可创建的文件后缀
    _CREATABLE_FILE_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".py", ".cfg", ".ini", ".toml", ".conf"})

    def _decide_from_error(
        self,
        result: SubAgentResult,
        task: SubAgentTask,
        session_history: list[SubAgentResult],
    ) -> dict:
        """基于 SubAgent 错误反馈动态决策下一步行动。

        决策逻辑：
        - permission_denied / timeout → skip（无法自动修复）
        - resource_missing + 可创建资源 → create_intermediate
        - resource_missing + 仅用户知道的资源 → ask_user
        - execution_failed + 无 missing → skip
        - execution_failed + 有 missing → 根据 missing 类型决定

        Returns:
            {
                "action": "create_intermediate" | "ask_user" | "skip",
                "reason": "决策原因",
                "intermediate_task": SubAgentTask | None,
                "ask_user_message": str | None,
            }
        """
        error_type = result.error_type or "execution_failed"

        # 1. 权限拒绝、超时 → 无法自动修复
        if error_type in ("permission_denied", "timeout"):
            return {
                "action": "skip",
                "reason": f"错误类型 {error_type} 无法自动修复",
                "intermediate_task": None,
                "ask_user_message": None,
            }

        # 2. 没有缺失资源信息 → 无法自动修复
        if not result.missing:
            return {
                "action": "skip",
                "reason": f"执行失败且无缺失资源信息: {result.error or '未知错误'}",
                "intermediate_task": None,
                "ask_user_message": None,
            }

        # 3. 分析缺失资源，判断哪些可自动创建、哪些需要询问用户
        creatable_missing: list[dict] = []
        user_only_missing: list[dict] = []

        for missing_item in result.missing:
            missing_type = missing_item.get("type", "")
            missing_path = missing_item.get("path", "")
            missing_reason = missing_item.get("reason", "")

            # 检查历史结果中是否已创建过相同文件
            if missing_path and self._already_created_in_history(missing_path, session_history):
                continue  # 已创建过，跳过

            if missing_type in self._CREATABLE_MISSING_TYPES:
                creatable_missing.append(missing_item)
            elif missing_type in self._USER_ONLY_MISSING_TYPES:
                user_only_missing.append(missing_item)
            elif missing_path:
                # 未知类型，根据文件后缀判断
                suffix = Path(missing_path).suffix.lower() if "." in missing_path else ""
                if suffix in self._CREATABLE_FILE_SUFFIXES:
                    creatable_missing.append(missing_item)
                else:
                    user_only_missing.append(missing_item)
            else:
                # 无 type 也无 path → 需询问用户
                user_only_missing.append(missing_item)

        # 4. 如果有仅用户知道的缺失信息 → ask_user
        if user_only_missing:
            ask_parts = []
            for m in user_only_missing:
                reason = m.get("reason", "") or m.get("path", "") or str(m)
                ask_parts.append(reason)
            return {
                "action": "ask_user",
                "reason": f"缺少只有用户才知道的信息: {', '.join(ask_parts[:3])}",
                "intermediate_task": None,
                "ask_user_message": "执行过程中发现缺少必要信息，请补充以下内容：\n" + "\n".join(
                    f"- {p}" for p in ask_parts
                ),
            }

        # 5. 如果有可创建的缺失资源 → create_intermediate
        if creatable_missing:
            # 合并所有可创建资源为一个前置任务
            descriptions = []
            for m in creatable_missing:
                path = m.get("path", "")
                reason = m.get("reason", "")
                descriptions.append(f"{path}（{reason}）" if path else reason)
            pre_task = SubAgentTask(
                sub_agent_type=SubAgentType.CODE_SCRIPT,
                task_description=f"创建缺失的资源文件: {', '.join(descriptions[:5])}",
                skill_sections=[],
                resources=[],
                depends_on=[],
            )
            return {
                "action": "create_intermediate",
                "reason": f"缺失可创建资源: {', '.join(descriptions[:3])}",
                "intermediate_task": pre_task,
                "ask_user_message": None,
            }

        # 6. 兜底：skip
        return {
            "action": "skip",
            "reason": f"无法自动修复: error_type={error_type}",
            "intermediate_task": None,
            "ask_user_message": None,
        }

    @staticmethod
    def _already_created_in_history(path: str, history: list[SubAgentResult]) -> bool:
        """检查历史结果中是否已创建过指定路径的文件。"""
        path_lower = path.lower().replace("\\", "/")
        for r in history:
            if not r.success:
                continue
            for f in (r.output_files or []):
                fpath = str(f.get("path", "")).lower().replace("\\", "/")
                if fpath and fpath.endswith(path_lower.split("/")[-1]):
                    return True
        return False

    async def _execute_intermediate_task(
        self,
        pre_task: SubAgentTask,
        skill_context: dict,
        request: ChatRequest,
        execution_root: Path | None,
        yield_func,
    ) -> SubAgentResult:
        """执行前置中间任务。"""
        executor = SubAgentExecutor(pre_task.sub_agent_type)
        if yield_func:
            yield_func(_master_event(
                "master_replan",
                task_id=pre_task.task_id,
                sub_agent_type=pre_task.sub_agent_type.value,
                reason=pre_task.task_description[:200],
            ))
        return await executor.execute(
            pre_task,
            skill_context,
            request,
            execution_root=execution_root,
            prior_results=None,
            yield_func=yield_func,
        )

    async def _retry_with_prior(
        self,
        task: SubAgentTask,
        skill_context: dict,
        request: ChatRequest,
        execution_root: Path | None,
        prior_results: list[SubAgentResult],
        yield_func,
    ) -> SubAgentResult:
        """带前置结果重试原任务。"""
        executor = SubAgentExecutor(task.sub_agent_type)
        if yield_func:
            yield_func(_master_event(
                "master_retry",
                task_id=task.task_id,
                sub_agent_type=task.sub_agent_type.value,
                reason="前置任务已完成，重试执行",
            ))
        return await executor.execute(
            task,
            skill_context,
            request,
            execution_root=execution_root,
            prior_results=prior_results if prior_results else None,
            yield_func=yield_func,
        )

    def _validate_decompose_response(self, parsed: dict) -> tuple[bool, str]:
        """校验 decompose LLM 输出是否符合预期 schema。

        LLM 输出被视为不可信输入，必须通过 schema 校验才能进入业务逻辑。
        检测错误阶段的响应（如 metadata_decision 风格）和字段缺失。

        Returns:
            (is_valid, reason): 是否有效 + 无效原因
        """
        if not isinstance(parsed, dict):
            return False, "输出不是 JSON object"

        # 检测是否是错误阶段的响应（metadata_decision 风格）
        if "need_body" in parsed and "sub_agent_tasks" not in parsed:
            return False, "检测到 metadata_decision 风格响应（包含 need_body 但缺少 sub_agent_tasks），可能是阶段混淆"

        # 检测是否是 resource_selection 风格响应
        if "need_resources" in parsed and "sub_agent_tasks" not in parsed:
            return False, "检测到 resource_selection 风格响应（包含 need_resources 但缺少 sub_agent_tasks），可能是阶段混淆"

        # 校验 sub_agent_tasks 字段
        tasks = parsed.get("sub_agent_tasks")
        if tasks is None:
            return False, "缺少 sub_agent_tasks 字段"
        if not isinstance(tasks, list):
            return False, "sub_agent_tasks 不是数组"
        if len(tasks) == 0:
            return False, "sub_agent_tasks 为空数组"

        # 校验每个 task 的必要字段
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                return False, f"task[{i}] 不是 JSON object"
            if not task.get("task_description"):
                return False, f"task[{i}] 缺少 task_description"
            if not task.get("sub_agent_type"):
                return False, f"task[{i}] 缺少 sub_agent_type"

        return True, ""

    def _infer_fallback_agent_type(self, metadata_prompt: str, body_prompt: str) -> SubAgentType:
        """根据 Skill 元数据和正文推断 fallback SubAgent 类型。

        当 LLM decompose 失败时，基于 Skill 自身的领域信息选择最合适的 SubAgent 类型，
        而非硬编码 CODE_SCRIPT。
        """
        combined = (metadata_prompt + "\n" + body_prompt).lower()

        # 数据库相关关键词（优先级最高）
        db_keywords = ["数据库", "sql", "mysql", "postgresql", "sqlite", "select",
                       "query", "db_query", "数据库查询", "数据统计"]
        if any(kw in combined for kw in db_keywords):
            return SubAgentType.DATA_QUERY

        # 文档生成相关关键词
        doc_keywords = ["pdf", "excel", "word", "ppt", "文档生成", "报告生成",
                        "格式转换", "fairy_tale", "build_pdf"]
        if any(kw in combined for kw in doc_keywords):
            return SubAgentType.DOCUMENT

        # 网络相关关键词
        net_keywords = ["http", "api", "curl", "webhook", "爬虫", "网络请求"]
        if any(kw in combined for kw in net_keywords):
            return SubAgentType.NETWORK_API

        # 默认 CODE_SCRIPT（通用脚本执行）
        return SubAgentType.CODE_SCRIPT

    def _build_fallback_plan(self, user_text: str, agent_type: SubAgentType) -> MasterPlan:
        """构建智能 fallback 执行计划。

        fallback 被视为一等公民策略，mode 标记为 single_agent 便于观察。
        """
        return MasterPlan(
            sub_agent_tasks=[
                SubAgentTask(
                    sub_agent_type=agent_type,
                    task_description=user_text,
                    skill_sections=[],
                    resources=[],
                    depends_on=[],
                )
            ],
            execution_order=[],
            mode="single_agent",
        )

    def _parse_decompose_response(self, response_text: str) -> dict:
        """解析 Master 调度 LLM 的输出。

        Returns:
            {"tasks": list[SubAgentTask], "need_ask_user": bool, "missing_info": list[str]}
        """
        try:
            stripped = _strip_markdown_json_fence(response_text)
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Master decompose response is not valid JSON: %s", response_text[:300])
            return {"tasks": [], "need_ask_user": False, "missing_info": []}

        if not isinstance(parsed, dict):
            return {"tasks": [], "need_ask_user": False, "missing_info": []}

        # 解析 need_ask_user 和 missing_info
        need_ask_user = bool(parsed.get("need_ask_user", False))
        missing_info = parsed.get("missing_info") or []
        if not isinstance(missing_info, list):
            missing_info = [str(missing_info)]
        else:
            missing_info = [str(m) for m in missing_info if m]

        raw_tasks = parsed.get("sub_agent_tasks") or []
        if not isinstance(raw_tasks, list):
            return {"tasks": [], "need_ask_user": need_ask_user, "missing_info": missing_info}

        tasks: list[SubAgentTask] = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                continue

            # 解析 sub_agent_type
            type_str = str(raw.get("sub_agent_type") or "").strip()
            try:
                sub_agent_type = SubAgentType(type_str)
            except ValueError:
                # 尝试模糊匹配
                sub_agent_type = self._fuzzy_match_agent_type(type_str)

            # 优先使用 LLM 提供的 task_id（如 "task_1"），否则自动生成
            # 这样 depends_on 中的引用才能正确匹配
            raw_task_id = str(raw.get("task_id") or "").strip()
            task_id = raw_task_id if raw_task_id else uuid.uuid4().hex[:12]

            tasks.append(SubAgentTask(
                task_id=task_id,
                sub_agent_type=sub_agent_type,
                task_description=str(raw.get("task_description") or ""),
                skill_sections=raw.get("skill_sections") or [],
                resources=raw.get("resources") or [],
                parameters=raw,
                depends_on=raw.get("depends_on") or [],
            ))

        return {"tasks": tasks, "need_ask_user": need_ask_user, "missing_info": missing_info}

    def _fuzzy_match_agent_type(self, type_str: str) -> SubAgentType:
        """模糊匹配 Agent 类型字符串。"""
        type_str = type_str.lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "data": SubAgentType.DATA_QUERY,
            "db": SubAgentType.DATA_QUERY,
            "database": SubAgentType.DATA_QUERY,
            "query": SubAgentType.DATA_QUERY,
            "sql": SubAgentType.DATA_QUERY,
            "code": SubAgentType.CODE_SCRIPT,
            "script": SubAgentType.CODE_SCRIPT,
            "shell": SubAgentType.CODE_SCRIPT,
            "python": SubAgentType.CODE_SCRIPT,
            "network": SubAgentType.NETWORK_API,
            "api": SubAgentType.NETWORK_API,
            "http": SubAgentType.NETWORK_API,
            "web": SubAgentType.NETWORK_API,
            "doc": SubAgentType.DOCUMENT,
            "document": SubAgentType.DOCUMENT,
            "pdf": SubAgentType.DOCUMENT,
            "excel": SubAgentType.DOCUMENT,
            "valid": SubAgentType.VALIDATION,
            "check": SubAgentType.VALIDATION,
            "review": SubAgentType.VALIDATION,
        }
        return aliases.get(type_str, SubAgentType.CODE_SCRIPT)

    def _topological_sort(self, tasks: list[SubAgentTask]) -> list[str]:
        """对任务列表进行拓扑排序。

        简化实现：按 depends_on 的深度排序。
        无依赖的任务排在前面，有依赖的任务排在后面。
        """
        task_ids = {t.task_id for t in tasks}
        # 构建邻接表
        in_degree: dict[str, int] = {t.task_id: 0 for t in tasks}
        for t in tasks:
            for dep in t.depends_on:
                if dep in task_ids:
                    in_degree[t.task_id] += 1

        # 按入度排序
        sorted_ids = sorted(in_degree.keys(), key=lambda tid: in_degree[tid])
        return sorted_ids


def _master_event(event_type: str, **data: Any) -> str:
    """构建 Master SSE 事件。"""
    from ..chat_utils import _sse
    return _sse({"type": event_type, **data})