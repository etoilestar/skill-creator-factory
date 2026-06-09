"""SOP 生成与计划确认。"""

import time as _time_module
import logging
from pathlib import Path

from ..chat_utils import _task_checklist

logger = logging.getLogger(__name__)


def _generate_sop_from_plan(
    *,
    instruction_analysis: dict,
    runtime_plan: dict,
    skill_name: str = "",
) -> dict:
    """Convert a runtime plan + instruction analysis into a standardized SOP document."""
    tasks = runtime_plan.get("tasks") or []
    mode = runtime_plan.get("mode", "execute")

    steps = []
    for i, task in enumerate(tasks, 1):
        action = task.get("action", "")
        reason = task.get("reason", "")
        command = task.get("command", "")
        path = task.get("path", "")

        step_name = ""
        description = reason or ""
        inputs = []
        outputs = []

        if action == "read_resource":
            step_name = f"读取资源：{path or task.get('resource_handle', '')}"
            inputs.append(path or task.get("resource_handle", ""))
        elif action == "run_command":
            step_name = f"执行命令"
            description = command[:200] if command else reason
            inputs.append(command[:100] if command else "")
        elif action == "write_file":
            step_name = f"写入文件：{path}"
            outputs.append(path)
        elif action == "create_directory":
            step_name = f"创建目录：{path}"
            outputs.append(path)
        else:
            step_name = action

        steps.append({
            "order": i,
            "name": step_name,
            "description": description,
            "action": action,
            "inputs": inputs,
            "outputs": outputs,
            "responsible": "agent",
        })

    # Build mermaid flowchart
    mermaid_lines = ["graph TD"]
    for i, step in enumerate(steps):
        node_id = f"S{step['order']}"
        label = step["name"][:30].replace('"', "'")
        mermaid_lines.append(f'    {node_id}["{label}"]')
        if i > 0:
            prev_id = f"S{steps[i-1]['order']}"
            mermaid_lines.append(f"    {prev_id} --> {node_id}")

    return {
        "title": f"SOP：{instruction_analysis.get('intent', skill_name)[:50]}",
        "version": "1.0",
        "skill_name": skill_name,
        "mode": mode,
        "complexity": instruction_analysis.get("complexity", "moderate"),
        "steps": steps,
        "total_steps": len(steps),
        "flowchart_mermaid": "\n".join(mermaid_lines),
    }


# ---------------------------------------------------------------------------
# Plan Confirmation Store (in-memory for simplicity)
# ---------------------------------------------------------------------------

# Stores pending plans awaiting user confirmation: { plan_id: { plan, skill_context, request, ts } }
_pending_plans: dict[str, dict] = {}
_PLAN_EXPIRY_SECONDS = 600  # plans expire after 10 minutes


def _cleanup_expired_plans():
    """Remove expired pending plans."""
    now = _time_module.time()
    expired = [k for k, v in _pending_plans.items() if now - v.get("ts", 0) > _PLAN_EXPIRY_SECONDS]
    for k in expired:
        del _pending_plans[k]


def _format_task_checklist_markdown(tasks: list[dict], *, instruction_analysis: dict | None = None) -> str:
    """Format a task list as a Markdown checklist for inline display in chat bubbles.

    Uses `- [ ]` / `- [x]` syntax that can be rendered by ChatBubble.
    This is the structured task checklist format shown in the planning mode
    conversation bubble, distinct from the detailed side panel view.
    """
    lines: list[str] = []

    if instruction_analysis:
        intent = instruction_analysis.get("intent", "")
        complexity = instruction_analysis.get("complexity", "")
        if intent:
            lines.append(f"**任务意图**：{intent}")
        if complexity:
            lines.append(f"**复杂度**：{complexity}")
        lines.append("")

    lines.append(f"**待执行任务清单**（共 {len(tasks)} 项）：")
    lines.append("")

    for idx, task in enumerate(tasks):
        action = str(task.get("action") or "").strip()
        reason = str(task.get("reason") or "").strip()

        # Build a concise description for each task
        if action == "run_command":
            cmd = str(task.get("command") or "")
            desc = f"执行命令：`{cmd[:80]}{'…' if len(cmd) > 80 else ''}`"
        elif action == "write_file":
            path = str(task.get("path") or "")
            desc = f"写入文件：`{path}`"
        elif action == "read_resource":
            path = str(task.get("path") or task.get("resource_handle") or "")
            desc = f"读取资源：`{path}`"
        elif action == "create_directory":
            path = str(task.get("path") or "")
            desc = f"创建目录：`{path}`"
        elif action in {"display", "ignore"}:
            desc = reason or action
        else:
            desc = reason or action

        lines.append(f"- [ ] {desc}")

    return "\n".join(lines)


# Public alias
format_task_checklist_markdown = _format_task_checklist_markdown
