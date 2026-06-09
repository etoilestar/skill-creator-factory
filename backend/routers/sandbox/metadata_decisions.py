"""元数据与子技能决策。"""

import json
import logging
import re

from ...services.llm_proxy import complete_chat_once
from ..chat_utils import (
    _request_messages_with_files,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest

logger = logging.getLogger(__name__)


def _parse_need_body_decision(text: str) -> bool:
    """Parse first-round metadata decision.

    解析失败时默认进入正文阶段，避免模型格式错误导致 Skill 无法执行。
    """
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("metadata decision is not valid JSON: %s", text[:500])
        return True

    need_body = data.get("need_body", True)

    if isinstance(need_body, bool):
        return need_body

    if isinstance(need_body, str):
        return need_body.strip().lower() in {"true", "1", "yes", "y"}

    return bool(need_body)

def _parse_child_skill_decision(
    text: str,
    *,
    valid_child_refs: set[str] | None = None,
) -> dict:
    """Parse child-skill loading decision.

    关键规则：
    - 只有 child_ref 出现在 Child Skills Manifest 的真实 ref 中，才允许 need_child=true。
    - 模型复制示例 ref 或猜测不存在 ref 时，一律降级为 need_child=false。
    """
    valid_child_refs = valid_child_refs or set()
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("child skill decision is not valid JSON: %s", text[:500])
        return {"need_child": False, "child_ref": "", "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"need_child": False, "child_ref": "", "reason": "输出不是 JSON object"}

    need_child = data.get("need_child", False)

    if isinstance(need_child, str):
        need_child = need_child.strip().lower() in {"true", "1", "yes", "y"}
    else:
        need_child = bool(need_child)

    child_ref = str(data.get("child_ref") or "").strip()
    reason = str(data.get("reason") or "").strip()

    if not need_child:
        return {"need_child": False, "child_ref": "", "reason": reason}

    if not child_ref:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": "need_child=true 但缺少 child_ref",
        }

    if child_ref not in valid_child_refs:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": (
                "模型返回的 child_ref 不在 Child Skills Manifest 中，已忽略："
                + child_ref
            ),
        }

    return {
        "need_child": True,
        "child_ref": child_ref,
        "reason": reason,
    }

def _extract_child_refs_from_metadata_prompt(metadata_prompt: str) -> set[str]:
    """Extract valid child skill refs from Child Skills Manifest.

    只信任 metadata prompt 中真实出现的：
    - ref: `xxx`
    """
    refs: set[str] = set()

    marker = "## Child Skills Manifest"
    index = metadata_prompt.find(marker)
    if index < 0:
        return refs

    section = metadata_prompt[index:]

    # 截到下一个 markdown 分隔符，避免误扫后面的 resource manifest
    next_sep = section.find("\n---\n")
    if next_sep >= 0:
        section = section[:next_sep]

    for match in re.finditer(r"-\s+ref:\s+`([^`]+)`", section):
        ref = match.group(1).strip()
        if ref and ref != "无":
            refs.add(ref)

    return refs

async def _run_metadata_round(
    *,
    metadata_prompt: str,
    request: ChatRequest,
    model: str,
) -> bool:
    """First internal model round.

    这一轮只给模型 metadata，不给 SKILL.md 正文。
    不向前端流式输出，只用于决定是否进入正文阶段。
    """
    messages = [{"role": "system", "content": metadata_prompt}]
    messages.extend(_request_messages_with_files(request))

    decision_text = await complete_chat_once(messages, model)
    return _parse_need_body_decision(decision_text)

def _compose_child_skill_selection_prompt() -> str:
    return (
        "你是 Skill 分层加载运行时的子 Skill 选择器。\n\n"
        "你会看到父 Skill 的 metadata prompt、valid_child_refs 和用户请求。\n"
        "你的任务是根据用户请求判断是否需要加载某一个子 Skill 的完整 SKILL.md 正文。\n\n"
        "重要规则：\n"
        "1. 只能从 valid_child_refs 中选择 child_ref。\n"
        "2. 如果 valid_child_refs 为空，必须 need_child=false。\n"
        "3. Child Skill 必须是包含 SKILL.md 的子目录，不是普通 references/*.md 文件。\n"
        "4. references/*.md、assets/*、scripts/* 都不是子 Skill，不能作为 child_ref 返回。\n"
        "5. 如果用户请求只需要父 Skill 就能完成，need_child=false。\n"
        "6. 如果用户请求明显匹配某个子 Skill 的 description，need_child=true，并返回 valid_child_refs 中的原样 ref。\n"
        "7. 不要猜测不存在的 ref。\n"
        "8. 不要复制示例占位符。\n"
        "9. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "如果需要子 Skill，输出：\n"
        "{\n"
        "  \"need_child\": true,\n"
        "  \"child_ref\": \"<必须是 valid_child_refs 中的一个值>\",\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n\n"
        "如果不需要子 Skill，输出：\n"
        "{\n"
        "  \"need_child\": false,\n"
        "  \"child_ref\": \"\",\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n"
    )

async def _run_child_skill_selection_round(
    *,
    parent_metadata_prompt: str,
    request: ChatRequest,
    model: str,
) -> dict:
    """Decide whether a child Skill body should be loaded.

    这一轮只使用父 Skill metadata prompt 中的 Child Skills Manifest。
    不读取子 Skill 正文。
    """
    valid_child_refs = _extract_child_refs_from_metadata_prompt(parent_metadata_prompt)

    if not valid_child_refs:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": "Child Skills Manifest 中没有可用子 Skill",
        }

    messages = [
        {"role": "system", "content": _compose_child_skill_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "valid_child_refs": sorted(valid_child_refs),
                    "parent_metadata_prompt": parent_metadata_prompt,
                    "user_messages": _request_messages_with_files(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    decision_text = await complete_chat_once(messages, model)
    return _parse_child_skill_decision(
        decision_text,
        valid_child_refs=valid_child_refs,
    )


# Public aliases
parse_need_body_decision = _parse_need_body_decision
parse_child_skill_decision = _parse_child_skill_decision
