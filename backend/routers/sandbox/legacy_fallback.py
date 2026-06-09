"""旧版 Markdown 块执行兜底。"""

import asyncio
import functools

from ..chat_utils import (
    _planner_model_name,
    _extract_all_fenced_blocks,
)
from .final_answer import _run_block_planner_round
from .task_executor import _execute_planned_actions
from ..chat_models import ChatRequest

logger = __import__("logging").getLogger(__name__)


async def _plan_and_execute_generated_output(
    *,
    assistant_text: str,
    request: ChatRequest,
    model: str,
    require_confirmation: bool = True,
    execution_root=None,
    skill_name: str = "",
) -> dict:
    """Legacy fallback: plan and execute actions from main model Markdown output.

    新主路径不再依赖这个函数。
    仅当 runtime planner 判断 direct_answer，或者旧 Skill 仍要求通过主模型 Markdown 输出动作时，才作为兜底。
    """
    from pathlib import Path
    blocks = _extract_all_fenced_blocks(assistant_text)

    if not blocks:
        return {
            "executed": False,
            "reason": "主模型回复中未检测到 fenced code block。",
            "plan": {"tasks": [], "errors": []},
            "results": [],
        }

    planner_model = _planner_model_name(model)

    plan = await _run_block_planner_round(
        assistant_text=assistant_text,
        blocks=blocks,
        request=request,
        model=planner_model,
    )

    if plan.get("errors") and not plan.get("tasks"):
        return {
            "executed": False,
            "reason": "规划模型未生成可执行任务。",
            "plan": plan,
            "results": [],
        }

    return await asyncio.to_thread(
        functools.partial(
            _execute_planned_actions,
            plan,
            blocks,
            request,
            require_confirmation=require_confirmation,
            execution_root=execution_root,
            skill_name=skill_name,
        )
    )


# Public alias
plan_and_execute_generated_output = _plan_and_execute_generated_output
