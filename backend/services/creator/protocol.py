"""Stage protocol primitives shared by the Creator model pipeline.

This module deliberately knows nothing about file formats or individual skills.  It
only protects model transport, phase transitions, revisions, and contract state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


class StructuredOutputError(ValueError):
    def __init__(self, phase: str, reason: str = "invalid_structured_output") -> None:
        super().__init__(f"{phase}: {reason}")
        self.phase = phase
        self.reason = reason


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Find the first balanced JSON object while respecting JSON strings."""
    start = depth = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if depth == 0:
            if char != "{":
                continue
            start, depth = index, 1
            in_string = escaped = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value
    return None


def parse_structured_output(response: str, *, phase: str) -> dict[str, Any]:
    """Parse raw, fenced, or prose-wrapped JSON object model output."""
    text = str(response or "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass
    value = _first_json_object(text)
    if value is not None:
        return value
    raise StructuredOutputError(phase)


GENERATION_FAILED = "generation_failed"
INVALID_PHASE_TRANSITION = "invalid_phase_transition"
ALLOWED_PHASE_STATUSES: Mapping[str, frozenset[str]] = {
    "requirement_analysis": frozenset({"ready", "needs_clarification"}),
    "blueprint_generation": frozenset({"ready", "need_revision", "failed"}),
    "blueprint_repair": frozenset({"ready", "need_revision", "failed"}),
    "graph_generation": frozenset({"ready", "need_revision", "failed"}),
    "graph_repair": frozenset({"ready", "need_revision", "failed"}),
}


def validate_phase_status(payload: Mapping[str, Any], *, phase: str) -> dict[str, Any]:
    """Return a protocol error rather than allowing a model to change phase."""
    result = dict(payload)
    status = str(result.get("status") or "")
    allowed = ALLOWED_PHASE_STATUSES.get(phase)
    if allowed is not None and status not in allowed:
        return {
            "status": INVALID_PHASE_TRANSITION,
            "current_phase": phase,
            "returned_status": status,
        }
    if status == "need_revision":
        target = str(result.get("target") or "")
        if target not in {"blueprint", "contract_graph"}:
            return {
                "status": INVALID_PHASE_TRANSITION,
                "current_phase": phase,
                "returned_status": status,
            }
    return result


async def request_structured_output(
    call: Callable[[Sequence[dict[str, Any]]], Awaitable[str]],
    messages: Sequence[dict[str, Any]], *, phase: str,
) -> dict[str, Any]:
    """Call a model with one format-only retry and a non-throwing failure result."""
    active = list(messages)
    for attempt in range(2):
        raw = await call(active)
        try:
            return validate_phase_status(parse_structured_output(raw, phase=phase), phase=phase)
        except StructuredOutputError:
            if attempt == 0:
                active = [*active, {"role": "user", "content": "只输出符合schema的JSON，不要解释"}]
    return {"status": GENERATION_FAILED, "phase": phase, "reason": "invalid_structured_output"}


REPAIR_PHASE_CONSTRAINT = """当前处于repair阶段。需求理解阶段已经结束。
如果当前设计无法满足合同、Blueprint需要重新设计或Contract Graph需要重新生成，不要返回needs_clarification；必须返回 {"status":"need_revision","target":"blueprint|contract_graph","reason":"..."}。
repair阶段禁止重新分析用户需求、提出新的问题列表或自行决定重新规划。"""


def revision_event(*, source_phase: str, target: str, reason: str) -> dict[str, str]:
    return {"event": "request_revision", "source_phase": source_phase, "target": target, "reason": reason}


def revision_transitions(event: Mapping[str, Any], *, contract_id: str = "") -> list[dict[str, str]]:
    """Materialize the explicit two-hop revision route for orchestration/UI."""
    if event.get("event") != "request_revision":
        raise ValueError("expected request_revision event")
    source = str(event.get("source_phase") or "")
    target = str(event.get("target") or "")
    destination = "blueprint_generation" if target == "blueprint" else "graph_generation"
    return [
        log_transition(source, "revision_requested", contract_id=contract_id, status="revision_required", trigger="request_revision"),
        log_transition("revision_requested", destination, contract_id=contract_id, status="ready", trigger="user_confirm"),
    ]


def log_transition(phase_before: str, phase_after: str, *, contract_id: str, status: str, trigger: str) -> dict[str, str]:
    event = {"phase_before": phase_before, "phase_after": phase_after, "contract_id": contract_id, "status": status, "trigger": trigger}
    logger.info("[Creator][transition] %s", json.dumps(event, ensure_ascii=False))
    return event


class ContractState(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    FROZEN = "frozen"


class ErrorKind(StrEnum):
    PROTOCOL_ERROR = "protocol_error"
    REVISION_REQUIRED = "revision_required"
    FATAL_ERROR = "fatal_error"


def classify_result(payload: Mapping[str, Any]) -> ErrorKind | None:
    """Classify protocol outcomes without treating every failure as fatal."""
    status = str(payload.get("status") or "")
    if status in {GENERATION_FAILED, INVALID_PHASE_TRANSITION}:
        return ErrorKind.PROTOCOL_ERROR
    if status == "need_revision" or payload.get("event") == "request_revision":
        return ErrorKind.REVISION_REQUIRED
    if status in {"failed", "fatal_error"}:
        return ErrorKind.FATAL_ERROR
    return None


@dataclass
class ContractLifecycle:
    contract_id: str
    payload: dict[str, Any]
    state: ContractState = ContractState.DRAFT
    events: list[dict[str, str]] = field(default_factory=list)

    def review(self) -> None:
        if self.state != ContractState.DRAFT:
            raise ValueError("only draft contracts can be reviewed")
        self.state = ContractState.REVIEWED

    def freeze(self) -> None:
        if self.state != ContractState.REVIEWED:
            raise ValueError("only reviewed contracts can be frozen")
        self.state = ContractState.FROZEN

    def consume(self, phase: str) -> dict[str, Any]:
        if phase in {"skill_generation", "e2e_generation"} and self.state != ContractState.FROZEN:
            raise ValueError(f"{phase} requires a frozen contract")
        return dict(self.payload)

    def report_problem(self, *, source_phase: str, target: str, reason: str) -> dict[str, str]:
        event = revision_event(source_phase=source_phase, target=target, reason=reason)
        self.events.append(event)
        return event
