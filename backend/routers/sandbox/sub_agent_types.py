"""Master-SubAgent 多智能体架构 — 核心类型定义。

本模块定义了 Sandbox 多 Agent 执行架构所需的全部数据结构，
不依赖任何业务模块，仅提供纯数据类型。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SubAgentType(str, Enum):
    """SubAgent 领域类型枚举。"""

    DATA_QUERY = "data_query"      # 数据库查询、数据统计、数据导出
    CODE_SCRIPT = "code_script"    # Shell/Python/JS 脚本执行、文件操作
    NETWORK_API = "network_api"    # HTTP 请求、第三方接口、爬虫
    DOCUMENT = "document"          # Excel/PDF/Word 解析、格式导出
    VALIDATION = "validation"      # 输出校验、安全审查


@dataclass
class SubAgentPolicy:
    """SubAgent 的沙箱权限策略。

    每个 SubAgent 类型拥有独立的权限边界，
    用于在 task_executor 执行前校验操作合法性。
    """

    allow_network: bool = False
    allow_file_write: bool = False
    allow_subprocess: bool = False
    allow_db_write: bool = False
    allowed_tool_categories: list[str] = field(default_factory=list)
    allowed_tool_roles: list[str] = field(default_factory=list)
    max_timeout: int = 300

    def check_action(self, action: str, task: dict) -> None:
        """校验当前 SubAgent 是否允许执行该 action。

        Raises:
            PermissionError: 当 action 超出权限边界时抛出。
        """
        action = (action or "").strip()
        if action == "run_command" and not self.allow_subprocess:
            cmd = str(task.get("command") or "")[:100]
            raise PermissionError(
                f"[SubAgentPolicy] 当前 SubAgent 不允许执行命令: {cmd}"
            )
        if action == "write_file" and not self.allow_file_write:
            path = str(task.get("path") or "")
            raise PermissionError(
                f"[SubAgentPolicy] 当前 SubAgent 不允许写入文件: {path}"
            )
        if action == "create_directory" and not self.allow_file_write:
            path = str(task.get("path") or "")
            raise PermissionError(
                f"[SubAgentPolicy] 当前 SubAgent 不允许创建目录: {path}"
            )


@dataclass
class SubAgentTask:
    """Master 派发给 SubAgent 的任务。"""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sub_agent_type: SubAgentType = SubAgentType.CODE_SCRIPT
    task_description: str = ""
    skill_sections: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    parameters_list: list | None = None  # 合并任务时的参数列表
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "sub_agent_type": self.sub_agent_type.value,
            "task_description": self.task_description,
            "skill_sections": self.skill_sections,
            "resources": self.resources,
            "parameters": self.parameters,
            "parameters_list": self.parameters_list,
            "depends_on": self.depends_on,
        }


@dataclass
class SubAgentResult:
    """SubAgent 返回给 Master 的执行结果。"""

    sub_agent_type: SubAgentType = SubAgentType.CODE_SCRIPT
    task_id: str = ""
    success: bool = False
    output: str = ""
    output_files: list[dict] = field(default_factory=list)
    error: str | None = None
    observations: list[dict] = field(default_factory=list)
    execution_time_ms: int = 0
    # 缺失资源反馈通道
    missing: list[dict] = field(default_factory=list)  # [{"type": "config_file", "path": "...", "reason": "..."}]
    need_ask_user: bool = False  # 是否需要向用户询问信息
    ask_user_message: str = ""  # 需要向用户询问的具体内容
    # 原始执行数据（用于 SubAgent 间传递结构化数据）
    raw_stdout: str = ""  # 保留 run_command 的原始 stdout
    # 结构化错误反馈
    error_type: str = ""  # "resource_missing" | "execution_failed" | "permission_denied" | "timeout" | ""
    error_detail: dict = field(default_factory=dict)  # 结构化错误详情

    def to_dict(self) -> dict:
        return {
            "sub_agent_type": self.sub_agent_type.value,
            "task_id": self.task_id,
            "success": self.success,
            "output": self.output[:2000],
            "output_files": self.output_files,
            "error": self.error,
            "observations": self.observations,
            "execution_time_ms": self.execution_time_ms,
            "missing": self.missing,
            "need_ask_user": self.need_ask_user,
            "ask_user_message": self.ask_user_message,
            "error_type": self.error_type,
            "error_detail": self.error_detail,
        }

    def to_summary(self) -> str:
        """生成供后续 SubAgent 或 Master 使用的精简摘要。"""
        parts = [f"[{self.sub_agent_type.value}] task={self.task_id}"]
        if self.success:
            parts.append(f"成功: {self.output[:500]}")
        else:
            parts.append(f"失败: {self.error or '未知错误'}")
        if self.output_files:
            paths = [f.get("path", "") for f in self.output_files[:5]]
            parts.append(f"产出文件: {', '.join(paths)}")
        # 传递 observations 中的关键数据（截断避免过长）
        if self.observations:
            for obs in self.observations[:3]:
                obs_type = obs.get("type", "")
                if obs_type == "execution_result":
                    summary = obs.get("summary", "")[:300]
                    parts.append(f"执行结果: {summary}")
                elif obs_type == "direct_answer":
                    content = obs.get("content", "")[:300]
                    parts.append(f"直接回答: {content}")
        # 缺失资源信息
        if self.missing:
            missing_items = [f"{m.get('type','')}: {m.get('path','') or m.get('reason','')}" for m in self.missing[:3]]
            parts.append(f"缺失资源: {', '.join(missing_items)}")
        if self.need_ask_user:
            parts.append(f"需要用户输入: {self.ask_user_message[:200]}")
        # 结构化错误信息
        if self.error_type:
            parts.append(f"错误类型: {self.error_type}")
        if self.error_detail:
            detail_items = [f"{k}={v}" for k, v in list(self.error_detail.items())[:3]]
            parts.append(f"错误详情: {', '.join(detail_items)}")
        return "\n".join(parts)


@dataclass
class MasterPlan:
    """Master Agent 的执行计划。"""

    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    sub_agent_tasks: list[SubAgentTask] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)  # task_id 有序列表
    mode: str = "multi_agent"  # multi_agent | single_agent (fallback) | ask_user
    # 缺失资源与用户询问
    need_ask_user: bool = False  # 是否需要向用户询问信息
    missing_info: list[str] = field(default_factory=list)  # 需要向用户询问的信息列表

    def get_execution_order(self) -> list[SubAgentTask]:
        """按依赖拓扑排序返回任务列表。

        简化实现：按 execution_order 顺序返回，
        未在 execution_order 中的任务追加到末尾。
        """
        task_map = {t.task_id: t for t in self.sub_agent_tasks}
        ordered = []
        for tid in self.execution_order:
            if tid in task_map:
                ordered.append(task_map[tid])
        # 追加未在 order 中的任务
        seen = set(self.execution_order)
        for t in self.sub_agent_tasks:
            if t.task_id not in seen:
                ordered.append(t)
        return ordered
