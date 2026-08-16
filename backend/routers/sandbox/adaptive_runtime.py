"""Bounded, observation-driven adaptation for Sandbox runtime plans."""

from __future__ import annotations

import json
import logging
from typing import Any

from ...services.llm_proxy import complete_chat_once
from ..chat_utils import _planner_model_name, _strip_markdown_json_fence
from .resource_catalog import _resource_catalog_for_planner
from .runtime_execution_plan import RuntimePlanError, validate_runtime_execution_plan

POLICY_VERSION = "adaptive-runtime-policy/v1"
MAX_ADAPTIVE_DECISIONS = 4
MAX_PLAN_REVISIONS = 2
MAX_RETRY_PER_STEP = 1
ADAPTIVE_ACTIONS = {
    "continue", "replan_remaining", "retry_current", "ask_user",
    "stop_success", "stop_failure",
}
logger = logging.getLogger(__name__)


def empty_adaptive_policy() -> dict:
    """Return the inert companion policy used for graceful degradation."""
    return {"version": POLICY_VERSION, "checkpoints": []}


def validate_adaptive_policy(policy: dict, runtime_plan: dict) -> dict:
    """Validate the companion policy without interpreting checkpoint reasons."""
    if not isinstance(policy, dict) or policy.get("version") != POLICY_VERSION:
        raise RuntimePlanError("adaptive_policy_invalid_version", f"version must equal {POLICY_VERSION}")
    if set(policy) != {"version", "checkpoints"} or not isinstance(policy.get("checkpoints"), list):
        raise RuntimePlanError("adaptive_policy_invalid_protocol", "policy must contain only version and checkpoints")
    step_ids = {step.get("step_id") for step in runtime_plan.get("steps") or []}
    seen: set[str] = set()
    checkpoints = []
    for raw in policy["checkpoints"]:
        if not isinstance(raw, dict) or set(raw) - {"after_step_id", "reason"}:
            raise RuntimePlanError("adaptive_policy_invalid_checkpoint", "checkpoint contains forbidden fields")
        step_id = raw.get("after_step_id")
        if step_id not in step_ids:
            raise RuntimePlanError("adaptive_policy_unknown_step", f"unknown checkpoint step: {step_id}")
        if step_id in seen:
            raise RuntimePlanError("adaptive_policy_duplicate_checkpoint", f"duplicate checkpoint: {step_id}")
        if not isinstance(raw.get("reason", ""), str):
            raise RuntimePlanError("adaptive_policy_invalid_checkpoint", "reason must be a string")
        seen.add(step_id)
        checkpoints.append({"after_step_id": step_id, "reason": raw.get("reason", "")})
    return {"version": POLICY_VERSION, "checkpoints": checkpoints}


def validate_runtime_plan_revision(original_plan: dict, revised_plan: dict,
                                   completed_step_ids: list[str], action_schema: dict,
                                   input_envelope: dict | None = None,
                                   resource_catalog=None) -> dict:
    """Canonicalize a revision and prove that its completed prefix is immutable."""
    canonical = validate_runtime_execution_plan(revised_plan, action_schema, input_envelope, resource_catalog)
    original_by_id = {step["step_id"]: step for step in original_plan["steps"]}
    revised_steps = canonical["steps"]
    if [step["step_id"] for step in revised_steps[:len(completed_step_ids)]] != completed_step_ids:
        raise RuntimePlanError("completed_prefix_modified", "completed step order or identity changed")
    for step_id in completed_step_ids:
        if step_id not in original_by_id or original_by_id[step_id] != revised_steps[completed_step_ids.index(step_id)]:
            raise RuntimePlanError("completed_prefix_modified", f"completed step changed: {step_id}")
    return canonical


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(rendered) <= limit else rendered[:limit] + "…"


def _safe_observation_for_agent(*, step: dict, observation: dict, context: dict,
                                remaining_steps: list[dict], output_files: list[dict]) -> dict:
    """Return a bounded observation; never include file bodies or prior prompts."""
    completed = {}
    for step_id, output in (context.get("steps") or {}).items():
        values = output if isinstance(output, list) else [output]
        completed[step_id] = sorted({key for value in values if isinstance(value, dict) for key in value})
    return {
        "current_step": {"step_id": step["step_id"], "script_path": step["script_path"],
                         "description": _truncate(step.get("description", ""), 300)},
        "observation": {key: _truncate(value, 1500) for key, value in observation.items()},
        "completed_step_output_keys": completed,
        "remaining_plan": [{"step_id": item["step_id"], "script_path": item["script_path"],
                            "description": _truncate(item.get("description", ""), 200)} for item in remaining_steps[:12]],
        "output_files": [{key: _truncate(item.get(key), 300) for key in ("path", "name", "size", "mime_type") if key in item}
                         for item in output_files[:50] if isinstance(item, dict)],
    }


async def _plan_adaptive_policy_with_model(*, user_request: str, runtime_plan: dict,
                                           action_schema: dict, skill_md: str = "",
                                           references: dict | None = None,
                                           resource_catalog=None, model: str | None = None) -> dict:
    messages = [{"role": "system", "content": (
        "You plan observation checkpoints for a deterministic Sandbox plan. Output only JSON using "
        "adaptive-runtime-policy/v1. Add a checkpoint only when actual output is needed to decide the suffix. "
        "Do not output commands, paths, params, tools, or modify the runtime plan."
    )}, {"role": "user", "content": json.dumps({
        "user_request": user_request, "runtime_plan": runtime_plan, "action_schema": action_schema,
        "skill_md": skill_md, "references": references or {},
        "resource_catalog": _resource_catalog_for_planner(resource_catalog or []),
    }, ensure_ascii=False)}]
    try:
        raw = await complete_chat_once(messages, _planner_model_name(model))
        return validate_adaptive_policy(json.loads(_strip_markdown_json_fence(raw)), runtime_plan)
    except Exception as exc:
        logger.warning("adaptive policy planner unavailable; continuing deterministically: %s", exc)
        return empty_adaptive_policy()


async def _decide_after_observation(*, user_request: str, runtime_plan: dict,
                                    completed_step_ids: list[str], safe_observation: dict,
                                    action_schema: dict, adaptive_reason: str,
                                    trigger: str, model: str | None = None) -> dict:
    messages = [{"role": "system", "content": (
        "You are the bounded Sandbox adaptive decision model. Output only JSON. action must be one of "
        "continue, replan_remaining, retry_current, ask_user, stop_success, stop_failure. "
        "For replan_remaining include a complete revised_plan. Never output tool_call, command, shell, or code."
    )}, {"role": "user", "content": json.dumps({
        "user_request": user_request, "runtime_plan": runtime_plan,
        "completed_step_ids": completed_step_ids, "safe_observation": safe_observation,
        "action_schema": action_schema, "adaptive_reason": adaptive_reason, "trigger": trigger,
    }, ensure_ascii=False)}]
    raw = json.loads(_strip_markdown_json_fence(
        await complete_chat_once(messages, _planner_model_name(model))))
    if not isinstance(raw, dict) or raw.get("action") not in ADAPTIVE_ACTIONS:
        raise RuntimePlanError("adaptive_decision_invalid", "invalid adaptive action")
    allowed = {"action", "reason"}
    if raw.get("action") == "replan_remaining":
        allowed.add("revised_plan")
    elif raw.get("action") == "retry_current":
        allowed.add("bindings")
    elif raw.get("action") == "ask_user":
        allowed.add("missing")
    if set(raw) - allowed or not isinstance(raw.get("reason", ""), str):
        raise RuntimePlanError("adaptive_decision_invalid", "decision contains forbidden fields")
    if raw["action"] == "replan_remaining" and not isinstance(raw.get("revised_plan"), dict):
        raise RuntimePlanError("adaptive_decision_invalid", "replan requires revised_plan")
    if "bindings" in raw and not isinstance(raw["bindings"], dict):
        raise RuntimePlanError("adaptive_decision_invalid", "retry bindings must be an object")
    if "missing" in raw and not isinstance(raw["missing"], list):
        raise RuntimePlanError("adaptive_decision_invalid", "ask_user missing must be a list")
    return raw
