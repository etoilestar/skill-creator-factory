from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from .llm_proxy import complete_chat_once

logger = logging.getLogger(__name__)


@dataclass
class AttemptRecord:
    attempt: int
    output: str
    valid: bool
    feedback: str


def _strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()

    if stripped.startswith("```json"):
        stripped = stripped[len("```json"):].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        return stripped

    if stripped.startswith("```"):
        stripped = stripped[len("```"):].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        return stripped

    return stripped


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


async def validate_output(
    prompt_messages: list[dict],
    model_output: str,
    model: str,
) -> tuple[bool, str]:
    messages = list(prompt_messages)
    messages.append(
        {
            "role": "user",
            "content": json.dumps({"model_output": model_output}, ensure_ascii=False),
        }
    )

    decision_text = await complete_chat_once(messages, model)
    stripped = _strip_markdown_json_fence(decision_text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("output validation model returned invalid JSON: %s", decision_text[:300])
        return False, "校验模型没有返回合法 JSON"

    if not isinstance(data, dict):
        return False, "校验模型输出不是 JSON object"

    valid = _coerce_bool(data.get("valid", False))
    reason = str(data.get("reason") or data.get("feedback") or "").strip()

    if not reason:
        reason = "输出未满足格式要求" if not valid else "输出格式正确"

    return valid, reason


async def retry_with_validation(
    messages: list[dict],
    model: str,
    max_retries: int = 3,
    *,
    validator_messages: list[dict] | None = None,
) -> tuple[str, bool, list[AttemptRecord]]:
    attempts: list[AttemptRecord] = []
    retries = max(1, int(max_retries))
    last_output = ""

    for attempt in range(1, retries + 1):
        output = await complete_chat_once(messages, model)
        last_output = output

        if validator_messages:
            valid, feedback = await validate_output(validator_messages, output, model)
        else:
            valid, feedback = True, ""

        attempts.append(
            AttemptRecord(
                attempt=attempt,
                output=output,
                valid=valid,
                feedback=feedback,
            )
        )

        if valid:
            return output, True, attempts

        messages = messages + [
            {"role": "assistant", "content": output},
            {
                "role": "user",
                "content": (
                    "你的上一次输出未满足格式规范要求。\n"
                    f"原因：{feedback}\n"
                    "请严格按照格式要求重新输出，不要添加额外说明。"
                ),
            },
        ]

    logger.warning("output validation failed after %d attempts", retries)
    return last_output, False, attempts
