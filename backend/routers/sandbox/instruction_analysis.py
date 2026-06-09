"""用户指令语义分析。"""

import json
import logging

from ...services.llm_proxy import complete_chat_once
from ..chat_utils import (
    _last_user_text,
    _planner_model_name,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest

logger = logging.getLogger(__name__)


def _compose_instruction_analysis_prompt() -> str:
    """System prompt for the instruction semantic analysis round."""
    return (
        "你是指令语义分析器。你的任务是精准识别用户自然语言指令的任务意图、执行范围、约束条件和输出要求。\n\n"
        "【重要】你只能输出一个严格的 JSON 对象，绝对不能输出任何自然语言、解释或 Markdown。\n\n"
        "分析维度：\n"
        "1. intent：用户想要完成的核心任务（一句话描述）\n"
        "2. scope：任务的执行范围（涉及哪些数据、文件、对象）\n"
        "3. constraints：约束条件列表（格式要求、数量限制、时间约束等）\n"
        "4. output_requirements：输出要求列表（文件格式、内容结构、展示形式等）\n"
        "5. complexity：任务复杂度 simple|moderate|complex\n"
        "   - simple：单步即可完成（直接回答/单个命令）\n"
        "   - moderate：需2-3步有序执行\n"
        "   - complex：需多步骤、多资源、有依赖的复合任务\n"
        "6. requires_script_execution：是否需要执行脚本或命令（true/false）\n"
        "   - 当用户请求涉及运行程序、调用工具、执行脚本、数据处理、文件转换时为 true\n"
        "   - 当用户请求仅需文本回答、查询信息时为 false\n\n"
        "输出格式：\n"
        "{\n"
        '  "intent": "任务意图描述",\n'
        '  "scope": "执行范围描述",\n'
        '  "constraints": ["约束1", "约束2"],\n'
        '  "output_requirements": ["输出要求1", "输出要求2"],\n'
        '  "complexity": "simple|moderate|complex",\n'
        '  "requires_script_execution": true\n'
        "}\n"
    )


async def _run_instruction_analysis_round(
    *,
    body_prompt: str,
    request: "ChatRequest",
    model: str,
) -> dict:
    """Analyze user instruction semantics and return structured understanding."""
    user_text = _last_user_text(request)
    messages = [
        {"role": "system", "content": _compose_instruction_analysis_prompt()},
        {"role": "user", "content": (
            f"## Skill 上下文摘要\n{body_prompt[:2000]}\n\n"
            f"## 用户指令\n{user_text}"
        )},
    ]

    planner_model = _planner_model_name(model)
    result_text = await complete_chat_once(messages, planner_model)
    stripped = _strip_markdown_json_fence(result_text)

    try:
        analysis = json.loads(stripped)
    except json.JSONDecodeError:
        analysis = {
            "intent": user_text[:200],
            "scope": "未能解析",
            "constraints": [],
            "output_requirements": [],
            "complexity": "moderate",
        }

    # Ensure required keys exist
    for key in ("intent", "scope", "constraints", "output_requirements", "complexity", "requires_script_execution"):
        if key not in analysis:
            if key in ("constraints", "output_requirements"):
                analysis[key] = []
            elif key == "requires_script_execution":
                # Default to false for safety; let the planner decide
                analysis[key] = False
            else:
                analysis[key] = ""

    # Normalize requires_script_execution to bool
    rse = analysis.get("requires_script_execution")
    if isinstance(rse, str):
        analysis["requires_script_execution"] = rse.strip().lower() in {"true", "1", "yes", "y"}
    elif not isinstance(rse, bool):
        analysis["requires_script_execution"] = bool(rse)

    return analysis
