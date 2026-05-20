"""Creator-mode chat helpers and routes.

State machine overview:
  A (需求收集) -> B (蓝图输出/修订) -> C (确认创建)
  - 状态 A: 收集缺失槽位信息，只允许输出一个问题
  - 状态 B: 输出或修订蓝图，等待明确确认语
  - 状态 C: 用户确认后进入创建阶段（由前端接管文件生成）
"""

import json
import re

from fastapi import APIRouter, HTTPException

from ..services.kernel_loader import (
    load_child_skill_body_prompt,
    load_kernel_body_prompt,
    load_kernel_metadata_prompt,
)
from .chat_models import ChatRequest, CreatorRequirementAnalysis, CreatorStateContext

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 明确确认语：进入状态 C 的硬性开关
_CONFIRM_KEYWORDS = (
    "对，开始做吧",
    "开始做吧",
    "开始创建",
    "开始生成",
    "开始制作",
    "开始干吧",
    "确认，开始",
    "确认开始",
    "可以开始",
    "没问题，开始",
)

# Marker written by the model when it outputs a blueprint (state B).
_BLUEPRINT_MARKERS = ("📋 Skill 蓝图",)
_CREATOR_PATTERN_CONTEXT_CHARS = 40
_CREATOR_VALIDATION_MAX_RETRIES = 3

_CREATOR_INPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(输入|用户会提供|用户输入|接收|读取|上传|原始数据|原文|素材|文本|文件|参数)"),
    re.compile(
        rf"(根据|基于|把|将).{{0,{_CREATOR_PATTERN_CONTEXT_CHARS}}}(整理|转换|提取|生成|改写|总结|分类|分析)",
        re.DOTALL,
    ),
)
_CREATOR_OUTPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(输出|返回|生成|产出|给出|得到|结果|报告|摘要|内容|结论)"),
    re.compile(r"(整理成|转换成|提取出|生成出)"),
)
_CREATOR_SCENARIO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(例如|比如|场景|触发|真实例子|用户会说|示例)"),
)
_CREATOR_RESOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(脚本|script|references/|assets/|api|接口|数据库|环境变量|依赖|模型|文件处理|外部服务)", re.IGNORECASE),
    re.compile(r"(不需要|无需|只靠模型|纯提示词).{0,20}(脚本|api|接口|数据库|依赖|外部服务)", re.IGNORECASE),
)


def _last_user_text(request: ChatRequest) -> str:
    """Return the latest user utterance (or empty string)."""
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content or ""
    return ""


def _has_creation_confirmation(request: ChatRequest) -> bool:
    """Only enter state C after explicit confirmation keywords appear."""
    text = _last_user_text(request).strip()
    return any(keyword in text for keyword in _CONFIRM_KEYWORDS)


def _creator_user_texts(request: ChatRequest) -> list[str]:
    """Return non-empty user utterances in order."""
    return [
        (message.content or "").strip()
        for message in request.messages
        if message.role == "user" and (message.content or "").strip()
    ]


def _creator_has_slot(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    """Return True when any pattern indicates the slot is present."""
    return any(pattern.search(text) for pattern in patterns)


def _creator_has_follow_up_round(request: ChatRequest) -> bool:
    """Return True when user has answered at least one assistant follow-up."""
    seen_first_user = False
    saw_assistant_follow_up = False

    for message in request.messages:
        content = (message.content or "").strip()
        if not content:
            continue

        if message.role == "assistant":
            if seen_first_user and not any(marker in content for marker in _BLUEPRINT_MARKERS):
                saw_assistant_follow_up = True
            continue

        if message.role != "user":
            continue

        if not seen_first_user:
            seen_first_user = True
            continue

        if saw_assistant_follow_up:
            return True

    return False


def _build_creator_clarifying_question(missing_slot: str) -> str:
    """Return a deterministic single-question follow-up for state A."""
    shared_input_output_question = "好的，我先确认一个关键信息：用户实际会提供什么输入，它最终又应该输出什么结果？最好直接给我一条真实示例。"
    prompts = {
        "purpose": "好的，我先确认一个关键信息：这个 Skill 最核心要解决什么问题？请用一句话说清它最主要的用途。",
        "input": shared_input_output_question,
        "output": shared_input_output_question,
        "scenario": "好的，我先确认一个关键信息：请给我一个最典型的使用场景，最好是一句用户真的会说的话。",
        "resources": "好的，我先确认一个关键信息：这个 Skill 是否需要脚本、参考资料、外部 API、数据库或其他依赖配置？如果都不需要，也请直接说明。",
        "mandatory_follow_up": "好的，我再确认一个关键细节：这个 Skill 还有没有必须遵守的限制、偏好或交付要求？如果没有，也请直接说“没有”。",
    }
    return prompts.get(missing_slot, prompts["input"])


def _are_creator_requirements_complete(
    missing_slots: list[str], has_follow_up_round: bool
) -> bool:
    """Blueprint output is allowed only after all slots are covered."""
    return not missing_slots and has_follow_up_round


def _analyze_creator_requirements(request: ChatRequest) -> CreatorRequirementAnalysis:
    """Best-effort requirement-slot analysis for creator mode."""
    user_texts = _creator_user_texts(request)
    combined = "\n".join(user_texts)

    has_purpose = bool(combined)
    has_input = _creator_has_slot(_CREATOR_INPUT_PATTERNS, combined)
    has_output = _creator_has_slot(_CREATOR_OUTPUT_PATTERNS, combined)
    has_scenario = _creator_has_slot(_CREATOR_SCENARIO_PATTERNS, combined)
    has_resources = _creator_has_slot(_CREATOR_RESOURCE_PATTERNS, combined)
    has_follow_up_round = _creator_has_follow_up_round(request)

    collected_slots: list[str] = []
    missing_slots: list[str] = []

    for slot_name, present in (
        ("purpose", has_purpose),
        ("input", has_input),
        ("output", has_output),
        ("scenario", has_scenario),
        ("resources", has_resources),
    ):
        if present:
            collected_slots.append(slot_name)
        else:
            missing_slots.append(slot_name)

    ready_for_blueprint = _are_creator_requirements_complete(
        missing_slots, has_follow_up_round
    )
    if missing_slots:
        next_prompt_key = missing_slots[0]
    elif not has_follow_up_round:
        next_prompt_key = "mandatory_follow_up"
    else:
        next_prompt_key = ""

    return CreatorRequirementAnalysis(
        user_turns=len(user_texts),
        collected_slots=collected_slots,
        missing_slots=missing_slots,
        ready_for_blueprint=ready_for_blueprint,
        next_question=_build_creator_clarifying_question(next_prompt_key),
    )


def _detect_creator_state(request: ChatRequest) -> CreatorStateContext:
    """Detect the current creator state-machine position from conversation history."""
    blueprint_shown = any(
        msg.role == "assistant"
        and any(marker in (msg.content or "") for marker in _BLUEPRINT_MARKERS)
        for msg in request.messages
    )
    requirement_analysis = _analyze_creator_requirements(request)

    last_user = _last_user_text(request).strip()
    if blueprint_shown and any(kw in last_user for kw in _CONFIRM_KEYWORDS):
        return CreatorStateContext(
            state="C",
            blueprint_shown=True,
            requirements=requirement_analysis,
        )

    if blueprint_shown or requirement_analysis.ready_for_blueprint:
        return CreatorStateContext(
            state="B",
            blueprint_shown=blueprint_shown,
            requirements=requirement_analysis,
        )

    return CreatorStateContext(
        state="A",
        blueprint_shown=blueprint_shown,
        requirements=requirement_analysis,
    )


def _compose_creator_state_injection(
    state: str,
    *,
    blueprint_shown: bool = False,
    requirement_analysis: CreatorRequirementAnalysis | None = None,
) -> str:
    """Return a system-message string that tells the model its current state."""
    blueprint_marker = _BLUEPRINT_MARKERS[0]
    if state == "A":
        if requirement_analysis is None:
            raise RuntimeError(
                "Internal error: requirement_analysis is required when composing state A injection. "
                "Ensure _detect_creator_state() runs before _compose_creator_state_injection()."
            )
        missing_desc = "、".join(requirement_analysis.missing_slots) or "无"
        return (
            "【后端状态注入】当前状态：A（需求收集）\n\n"
            f"对话历史中尚未满足蓝图输出条件；当前缺失槽位：{missing_desc}。\n"
            "本轮必须处于状态 A，严格执行以下规则：\n"
            "1. 只允许输出一个简洁问题，询问当前最缺失的需求信息。\n"
            f"2. 禁止输出蓝图（{blueprint_marker}）。\n"
            "3. 禁止输出任何 fenced code block（```）。\n"
            "4. 禁止输出 SKILL.md、scripts/、references/、assets/ 的内容。\n"
            "5. 禁止说'我来帮你创建'、'以下是设计文档'、'下面是实现代码'等。\n"
            "6. 回复必须是一个自然语言问题，不要包含多段说明或列表。\n"
            f"建议提问方向（可改写）：{requirement_analysis.next_question}"
        )
    if state == "B":
        confirm_keywords = " / ".join(_CONFIRM_KEYWORDS)
        if not blueprint_shown:
            return (
                "【后端状态注入】当前状态：B（蓝图输出阶段）\n\n"
                "关键信息已收集完成，且用户至少完成了一轮补充说明。\n"
                "本轮必须只输出完整蓝图，不得输出任何文件内容、代码块、测试命令或创建报告。\n"
                "蓝图结尾必须使用“这是我理解的需求，对吗？”以及 A/B/C 三个确认选项。\n"
                f"其中“开始”选项必须包含以下触发语之一：{confirm_keywords}。"
            )
        return (
            "【后端状态注入】当前状态：B（蓝图已展示，等待用户确认）\n\n"
            "对话历史中已包含蓝图，但用户尚未发出确认语。\n"
            "本轮必须处于状态 B，严格执行以下规则：\n"
            "1. 禁止创建任何文件或目录。\n"
            "2. 禁止输出任何 SKILL.md 正文或脚本代码块。\n"
            "3. 如果用户要求修改蓝图，根据意见调整后重新展示完整蓝图，仍然以'这是我理解的需求，对吗？'结尾。\n"
            f"4. 提醒用户使用明确的触发语确认（例如：{confirm_keywords}）。\n"
            "5. 等待用户发出确认语后，后端才会解锁文件创建权限。"
        )
    # state == "C"
    return (
        "【后端状态注入】当前状态：C（创建阶段）\n\n"
        "用户已明确发出确认语，系统已进入创建阶段。\n"
        "本轮可以：创建目录、写入文件、输出 SKILL.md 和脚本代码块、执行校验、报告结果。\n"
        "按照蓝图逐步完成所有文件的创建，完成后给出简短报告。"
    )


def _compose_creator_validation_messages(
    state: str,
    *,
    requirement_analysis: CreatorRequirementAnalysis | None = None,
) -> list[dict]:
    """Build a validator prompt enforcing state-A/B output constraints."""
    if state == "A":
        rules = [
            "只能输出一个简短问题，不能出现多个问号或多段说明",
            "禁止包含任何蓝图标题（例如 '📋 Skill 蓝图' 或 '📋 Skill 架构蓝图'）",
            "禁止出现 fenced code block（``` 或 ~~~）",
            "禁止输出 SKILL.md、scripts/、references/、assets/ 的文件内容",
        ]
        payload = {
            "state": "A",
            "missing_slots": (requirement_analysis.missing_slots if requirement_analysis else []),
            "rules": rules,
        }
    else:
        confirm_keywords = list(_CONFIRM_KEYWORDS)
        rules = [
            "必须输出完整蓝图正文，包含蓝图标题（例如 '📋 Skill 蓝图'）",
            "蓝图结尾必须包含“这是我理解的需求，对吗？”以及三个确认选项",
            "确认选项中必须出现至少一个明确触发语（如 '开始制作'、'开始干吧'、'对，开始做吧'）",
            "禁止出现 fenced code block（``` 或 ~~~）",
            "禁止输出任何文件内容、脚本或执行命令",
        ]
        payload = {
            "state": "B",
            "confirm_keywords": confirm_keywords,
            "rules": rules,
        }

    return [
        {
            "role": "system",
            "content": (
                "你是输出格式校验器。\n"
                "请根据规则判断模型输出是否合格。\n"
                "只输出 JSON，不要解释：\n"
                "{\n"
                "  \"valid\": true/false,\n"
                "  \"reason\": \"不合格原因，合格时可简短说明\"\n"
                "}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _simple_sse_content_response(content: str) -> list[str]:
    """Return a minimal SSE response payload containing one assistant message."""
    from .chat import _sse  # local import avoids circular dependency at import time

    return [
        _sse({"status": None}),
        _sse({"content": content}),
        "data: [DONE]\n\n",
    ]


def _compose_creator_artifact_consistency_prompt() -> str:
    """Creator-stage consistency contract (used only in state B/C)."""
    return (
        "当前处于 Skill Creator 严格生成模式。\n\n"
        "你正在创建一个可运行的 Skill 包，而不是只写说明文档。"
        "你生成的所有文件必须形成一个自洽的整体，包括但不限于 SKILL.md、脚本、配置、参考文件和测试命令。\n\n"
        "一致性要求：\n"
        "1. 如果你生成了任何可执行入口、脚本、工具调用、配置入口或其他运行资源，"
        "同时又在说明文档中写出了调用方式、运行方式、示例命令或使用步骤，"
        "两者的输入接口必须严格一致。\n"
        "2. 说明文档中的调用方式必须由你生成的实际代码支持；"
        "实际代码接收输入的方式也必须能被说明文档中的调用方式触发。\n"
        "3. 如果代码通过命令行参数接收输入，说明文档中的调用方式必须使用对应的命令行参数。\n"
        "4. 如果代码通过标准输入、文件、环境变量、配置、HTTP 请求体、JSON 字段或其他方式接收输入，"
        "说明文档中的调用方式必须体现同一种输入通道。\n"
        "5. 如果说明文档要求某个参数、字段、文件、输入通道或调用步骤，实际代码必须实现它。\n"
        "6. 如果实际代码实现了某个输入通道，说明文档中的调用方式不得写成另一个不兼容的输入通道。\n"
        "7. 示例值、占位值和演示输入只能用于说明；当需要给出可执行调用示例时，"
        "必须保证该示例在当前生成的代码中真实可运行。\n\n"
        "禁止行为：\n"
        "1. 禁止只生成看起来合理但与代码入口不匹配的调用方式。\n"
        "2. 禁止文档写一种输入形式、代码实现另一种输入形式。\n"
        "3. 禁止依赖后台替你修正参数、命令或输入通道。\n"
        "4. 禁止假设宿主会自动把命令行参数转换成标准输入，或把标准输入转换成命令行参数。\n"
        "5. 禁止生成互相矛盾的 SKILL.md、脚本和测试命令。\n\n"
        "生成前自检：\n"
        "在输出写文件代码块之前，你必须在内部完成一致性检查：\n"
        "- 文档中的每个可执行调用是否被实际代码支持；\n"
        "- 实际代码需要的每个必要输入是否在文档调用方式中提供；\n"
        "- 示例调用是否能在当前 Skill 目录下直接运行；\n"
        "- 报错信息和输出要求是否与文档约束一致。\n\n"
        "输出要求：\n"
        "你仍然只输出普通 Markdown 和 fenced code block。"
        "不要输出自定义动作标签。"
        "需要写入文件时，仍应在代码块附近明确写出保存路径。"
    )


def build_kernel_skill_context() -> dict:
    """Build creator-mode skill context for the kernel Skill Creator."""
    kernel_metadata_prompt = load_kernel_metadata_prompt()

    return {
        "skill_name": "skill-creator",
        "metadata_prompt": kernel_metadata_prompt,
        "body_loader": load_kernel_body_prompt,
        "child_body_loader": lambda child_ref: load_child_skill_body_prompt("skill-creator", child_ref),
        "force_body": True,
        "enable_action_execution": True,
        "require_action_confirmation": True,
        "execution_root": None,
        "strict_creator_generation": True,
        "skip_runtime_planner_before_confirmation": True,
        "disable_runtime_planner": True,
        # creator 阶段按需读取 references/assets/scripts
        "enable_resource_preload": True,
        # 方案 C：state C 由前端面板主控文件生成，chat 端点只输出简短确认语
        "use_frontend_driven_creation": True,
    }


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Multi-turn chat powered by the fixed kernel Skill Creator."""
    from .chat import _make_stream  # local import avoids circular dependency

    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream(skill_context, request)
