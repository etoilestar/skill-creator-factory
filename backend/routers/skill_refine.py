"""Skill refinement endpoint — modify SKILL.md based on test feedback and save as new version."""

import json
import logging
import re
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services.llm_proxy import stream_chat
from ..services.model_router import route_model, TEXT_TASK
from ..services.skill_manager import get_skill, save_skill
from ..services.skill_metadata import parse_skill_frontmatter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skill-refine"])

_MAX_CONVERSATION_ROUNDS = 5  # 最近 N 轮对话作为反馈上下文
_REFINE_MAX_RETRIES = 1       # SKILL.md 格式校验失败最多重试次数


class RefineRequest(BaseModel):
    messages: list[dict]
    modification_request: str
    sandbox_session_id: str = ""


# ── SSE helpers ──────────────────────────────────────────────────────

def _sse_event(event_type: str, data: dict | None = None, text: str | None = None) -> str:
    if text is not None:
        return f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
    payload = {"type": event_type}
    if data is not None:
        payload["data"] = data
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_done() -> str:
    return "data: [DONE]\n\n"


# ── Prompt construction ──────────────────────────────────────────────

_REFINE_SYSTEM_PROMPT = """你是一个技能修改专家。你的任务是根据用户的测试反馈，修改 SKILL.md 的内容。

## 规则
1. 保持 frontmatter（---之间的YAML）的结构不变，只更新必要字段（如 version、description）
2. 修改 body 部分的指令内容，使其更好地满足用户需求
3. 如果用户要求增加功能，在 body 中添加对应的指令段落
4. 如果用户要求修改输出格式，更新 body 中关于输出格式的描述
5. 不要删除与用户修改要求无关的现有功能
6. 输出修改后的完整 SKILL.md，用 ```markdown 代码块包裹
7. 在代码块之前，先用自然语言简要说明你做了哪些修改以及为什么"""


def _build_refine_messages(
    current_skill_md: str,
    conversation_history: list[dict],
    modification_request: str,
) -> list[dict]:
    # 提取最近 N 轮对话作为反馈摘要
    recent = conversation_history[-_MAX_CONVERSATION_ROUNDS * 2:]  # user+assistant 各算一轮
    feedback_lines = []
    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            label = "用户" if role == "user" else "Skill"
            # 截断过长内容
            snippet = content[:500] + ("…" if len(content) > 500 else "")
            feedback_lines.append(f"[{label}]: {snippet}")

    feedback_text = "\n".join(feedback_lines) if feedback_lines else "（无历史对话）"

    user_prompt = f"""## 当前 SKILL.md 内容

```markdown
{current_skill_md}
```

## 用户测试反馈（最近对话）

{feedback_text}

## 用户本次修改要求

{modification_request}

请根据以上信息修改 SKILL.md，输出修改后的完整内容。"""

    return [
        {"role": "system", "content": _REFINE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


# ── Extract SKILL.md from LLM output ────────────────────────────────

def _extract_skill_md(llm_output: str) -> str | None:
    """从 LLM 输出中提取 ```markdown ... ``` 代码块内的 SKILL.md 内容。"""
    # 匹配 ```markdown 或 ``` 包裹的内容
    patterns = [
        r"```markdown\s*\n(.*?)```",
        r"```\s*\n(.*?)```",
    ]
    for pattern in patterns:
        match = re.search(pattern, llm_output, re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


# ── Main endpoint ────────────────────────────────────────────────────

@router.post("/api/skills/{skill_name}/refine")
async def refine_skill(skill_name: str, request: RefineRequest) -> StreamingResponse:
    """根据测试反馈修改技能 SKILL.md 并保存为新版本。"""

    # 1. 读取当前技能
    try:
        skill_info = get_skill(skill_name, mode="manage")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    current_content = skill_info.get("content", "")
    if not current_content:
        raise HTTPException(status_code=400, detail="Skill has no SKILL.md content")

    # 2. 选择模型
    model_route = route_model(TEXT_TASK, reason="skill_refine")
    model = model_route.model

    async def generate() -> AsyncGenerator[str, None]:
        # 状态：分析中
        yield _sse_event("status", {"phase": "analyzing", "message": "正在分析测试反馈…"})

        # 3. 构造 prompt
        refine_messages = _build_refine_messages(
            current_skill_md=current_content,
            conversation_history=request.messages,
            modification_request=request.modification_request,
        )

        # 状态：生成中
        yield _sse_event("status", {"phase": "generating", "message": "正在生成修改方案…"})

        # 4. 流式调用 LLM
        full_output_parts: list[str] = []
        try:
            async for chunk in stream_chat(refine_messages, model):
                full_output_parts.append(chunk)
                yield _sse_event(text=chunk)
        except Exception as e:
            logger.error("[refine] LLM stream error: %s", e)
            yield _sse_event("status", {"phase": "error", "message": f"LLM 调用失败: {e}"})
            yield _sse_done()
            return

        full_output = "".join(full_output_parts)

        # 5. 提取修改后的 SKILL.md
        new_skill_md = _extract_skill_md(full_output)

        if new_skill_md is None:
            # LLM 没有输出代码块，尝试将整个输出作为 SKILL.md（如果包含 frontmatter）
            if new_skill_md is None and "---" in full_output[:100]:
                # 可能没有代码块包裹，直接使用全文
                new_skill_md = full_output.strip()

            if new_skill_md is None:
                yield _sse_event("status", {"phase": "error", "message": "无法从 LLM 输出中提取 SKILL.md，请重试"})
                yield _sse_done()
                return

        # 6. 校验 frontmatter 格式
        try:
            meta = parse_skill_frontmatter(new_skill_md)
            if not meta.get("name"):
                raise ValueError("SKILL.md 缺少 name 字段")
        except Exception as e:
            logger.warning("[refine] Frontmatter validation failed: %s", e)
            yield _sse_event("status", {"phase": "error", "message": f"生成的 SKILL.md 格式无效: {e}"})
            yield _sse_done()
            return

        # 7. 保存新版本
        yield _sse_event("status", {"phase": "saving", "message": "正在保存新版本…"})

        try:
            result = save_skill(skill_name, new_skill_md)
            new_version = result.get("version", "unknown")
            logger.info("[refine] Skill '%s' saved as v%s", skill_name, new_version)
        except Exception as e:
            logger.error("[refine] Save error: %s", e)
            yield _sse_event("status", {"phase": "error", "message": f"保存失败: {e}"})
            yield _sse_done()
            return

        # 8. 发送完成事件
        yield _sse_event("refine_result", {
            "skill_name": skill_name,
            "new_version": new_version,
            "changes_summary": f"已根据测试反馈修改 SKILL.md 并保存为 v{new_version}",
        })

        yield _sse_done()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
