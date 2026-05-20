"""Model output validator with retry logic for the creator flow.

``validate_output``      — ask the same model whether its own output meets the
                           requirements stated in the original prompt messages.
``retry_with_validation`` — loop up to *max_retries* times, re-prompting on failure.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from .llm_proxy import complete_chat_once

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validator prompt
# ---------------------------------------------------------------------------

_VALIDATOR_SYSTEM = (
    "你是一个严格的输出格式校验器。\n"
    "你会收到一个 JSON 对象，包含以下字段：\n"
    "- instruction: 发给生成模型的原始系统提示\n"
    "- output: 模型的实际输出\n\n"
    "你的任务：仅根据 instruction 中明确提出的格式和结构要求，判断 output 是否合格。\n"
    "重点检查（按优先级）：\n"
    "1. 如果 instruction 要求输出 JSON，output 是否是合法且完整的 JSON？\n"
    "2. 如果 instruction 要求不含代码块（no ```），output 是否包含了多余的 ``` 包裹？\n"
    "3. output 是否看起来完整（非被截断、非空）？\n"
    "4. output 是否包含 instruction 明确禁止的内容？\n\n"
    "只关注格式合规性，不评价内容质量。\n"
    "只输出严格 JSON，不要任何解释：\n"
    '{"valid": true} 或 {"valid": false, "reason": "一句话原因"}'
)

# Maximum characters of instruction / output forwarded to the validator.
_MAX_INSTRUCTION_CHARS = 2000
_MAX_OUTPUT_CHARS = 3000


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AttemptRecord:
    """Record of a single generation attempt."""
    attempt: int
    output_chars: int
    is_valid: bool
    reason: str
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_validator_fence(text: str) -> str:
    """Remove code-fence wrappers a validator model may add despite instructions."""
    s = text.strip()
    if s.startswith("```"):
        s = s.lstrip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def validate_output(
    prompt_messages: list[dict],
    model_output: str,
    model: str,
) -> tuple[bool, str]:
    """Ask the same model to evaluate whether *model_output* satisfies *prompt_messages*.

    Returns ``(is_valid, reason)``.

    On any error the function returns ``(True, "校验器调用失败，默认通过")`` so that
    transient validator failures never silently block the creation flow.
    """
    # Extract the primary instruction from the first system message.
    instruction = ""
    for msg in prompt_messages:
        if msg.get("role") == "system":
            instruction = msg.get("content", "")
            break

    payload = json.dumps(
        {
            "instruction": instruction[:_MAX_INSTRUCTION_CHARS],
            "output": model_output[:_MAX_OUTPUT_CHARS],
        },
        ensure_ascii=False,
    )

    messages = [
        {"role": "system", "content": _VALIDATOR_SYSTEM},
        {"role": "user", "content": payload},
    ]

    try:
        result_text = await complete_chat_once(messages, model)
    except Exception as exc:  # pragma: no cover
        logger.warning("output validator LLM call failed: %s", exc)
        return True, "校验器调用失败，默认通过"

    text = _strip_validator_fence(result_text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("validator returned non-JSON: %.300s", result_text)
        return True, "校验结果解析失败，默认通过"

    if not isinstance(data, dict):
        return True, "校验结果格式错误，默认通过"

    valid = bool(data.get("valid", True))
    reason = str(data.get("reason", "")).strip() or "格式不符合要求"
    return valid, reason


async def retry_with_validation(
    messages: list[dict],
    model: str,
    *,
    max_retries: int = 3,
) -> tuple[str, bool, list[AttemptRecord]]:
    """Call the LLM up to *max_retries* times, validating each output.

    Returns ``(final_output, succeeded, attempt_log)``.

    - ``succeeded=True``  — a valid output was produced within the retry budget.
    - ``succeeded=False`` — all attempts failed; ``final_output`` is the last attempt.
    - ``attempt_log``     — list of :class:`AttemptRecord` for observability.
    """
    current_messages = list(messages)
    last_output = ""
    log: list[AttemptRecord] = []

    for attempt in range(1, max_retries + 1):
        logger.info(
            "retry_with_validation attempt %d/%d model=%s",
            attempt, max_retries, model,
        )

        try:
            output = await complete_chat_once(current_messages, model)
        except Exception as exc:
            logger.warning("LLM call failed on attempt %d: %s", attempt, exc)
            last_output = ""
            log.append(AttemptRecord(
                attempt=attempt,
                output_chars=0,
                is_valid=False,
                reason=f"LLM 调用失败: {exc}",
            ))
            if attempt < max_retries:
                current_messages = list(messages) + [
                    {"role": "user", "content": "上一次生成出现错误，请重试。"},
                ]
            continue

        last_output = output
        is_valid, reason = await validate_output(current_messages, output, model)
        logger.info(
            "validate attempt %d: valid=%s reason=%.120s",
            attempt, is_valid, reason,
        )
        log.append(AttemptRecord(
            attempt=attempt,
            output_chars=len(output),
            is_valid=is_valid,
            reason=reason,
        ))

        if is_valid:
            return output, True, log

        if attempt < max_retries:
            current_messages = list(messages) + [
                {"role": "assistant", "content": output},
                {
                    "role": "user",
                    "content": f"你的上一次输出不符合要求，原因：{reason}，请重试。",
                },
            ]

    return last_output, False, log
