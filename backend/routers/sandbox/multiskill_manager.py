"""High-level Multi-Skill decision, preview, execution, and safe synthesis."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from copy import deepcopy
from typing import Callable

from ...config import settings
from ...services.llm_proxy import complete_chat_once
from .child_skill_runtime import execute_child_skill_runtime
from .multiskill_catalog import (
    _plan_skill_candidates_with_model,
    build_multiskill_activation_cards,
    build_multiskill_catalog,
)
from .multiskill_executor import (
    build_single_skill_input_envelope,
    execute_multiskill_plan,
    normalize_child_skill_result,
)
from .multiskill_plan import plan_multiskill, validate_multiskill_plan

_pending_multiskill_plans: dict[str, dict] = {}
_MULTISKILL_PLAN_EXPIRY_SECONDS = 600


class PendingMultiSkillPlanError(ValueError):
    pass


def _cleanup_expired_multiskill_plans(*, now: float | None = None) -> None:
    current = time.time() if now is None else now
    for plan_id in [key for key, value in _pending_multiskill_plans.items()
                    if current - float(value.get("created_at", 0)) > _MULTISKILL_PLAN_EXPIRY_SECONDS]:
        del _pending_multiskill_plans[plan_id]


def _store_multiskill_plan(*, selection_mode: str, user_request: str, parent_envelope: dict,
                           model: str | None, plan: dict | None = None,
                           skill_name: str | None = None) -> str:
    _cleanup_expired_multiskill_plans()
    plan_id = f"multiskill_{uuid.uuid4().hex}"
    _pending_multiskill_plans[plan_id] = {
        "selection_mode": selection_mode, "plan": deepcopy(plan), "skill_name": skill_name,
        "user_request": user_request,
        "parent_envelope": deepcopy(parent_envelope), "model": model,
        "created_at": time.time(),
    }
    return plan_id


def _take_multiskill_plan(plan_id: str) -> dict:
    existed = plan_id in _pending_multiskill_plans
    _cleanup_expired_multiskill_plans()
    value = _pending_multiskill_plans.pop(plan_id, None)
    if value is None:
        raise PendingMultiSkillPlanError(
            "multiskill_plan_expired" if existed else "multiskill_plan_not_found"
        )
    return value


def preview_multiskill_plan(plan: dict) -> list[dict]:
    """Expose only auditable skill tasks and manifest-level data sources."""
    preview = []
    for step in plan["steps"]:
        sources = []
        for target, binding in step["bindings"].items():
            if binding.get("source_type") == "skill_result":
                source = f'{binding["step_id"]}.{binding["channel"]}'
                if binding.get("path"):
                    source += f'.{binding["path"]}'
                sources.append({"target": target, "source": source})
        preview.append({"step_id": step["step_id"], "skill_name": step["skill_name"],
                        "task": step["task"], "input_sources": sources})
    return preview


async def run_multiskill_manager(*, user_request: str, parent_envelope: dict, activation_cards: list[dict],
                                 single_skill_runtime: Callable | None = None, execution_mode="execute",
                                 confirmed_plan: dict | None = None, confirmed_skill_name: str | None = None,
                                 model_call=None, event_sink=None,
                                 final_synthesizer: Callable | None = None, model: str | None = None) -> dict:
    """Run one decision. Confirmed plans are validated and executed without replanning."""
    names = [card["skill_name"] for card in activation_cards]
    if confirmed_skill_name is not None:
        if confirmed_skill_name not in names:
            raise PermissionError("skill_not_executable")
        decision = {"mode": "single_skill", "skill_name": confirmed_skill_name}
    elif confirmed_plan is not None:
        decision = {"mode": "multi_skill", "plan": validate_multiskill_plan(confirmed_plan, names)}
    else:
        decision = await plan_multiskill(user_request=user_request,
            input_envelope_summary={key: bool(value) for key, value in parent_envelope.items()
                                    if not key.startswith("_")},
            activation_cards=activation_cards, model_call=model_call)
    if decision["mode"] == "single_skill":
        name = decision["skill_name"]
        if execution_mode == "plan":
            return {"success": True, "mode": "plan", "selection_mode": "single_skill",
                    "skill_name": name, "preview": [{"step_id": "skill_1", "skill_name": name,
                                                       "task": user_request}]}
        run_id = f"child_{uuid.uuid4().hex}"
        runtime = single_skill_runtime or execute_child_skill_runtime
        async def child_sink(event):
            if event_sink:
                value = event_sink({"child_runtime_event": {"child_run_id": run_id, "event": event}})
                if inspect.isawaitable(value):
                    await value
        await _emit_parent_event(event_sink, {"skill_started": {
            "step_id": "skill_1", "skill_name": name, "child_run_id": run_id}})
        child_envelope = build_single_skill_input_envelope(parent_envelope,
            skill_name=name, child_run_id=run_id)
        raw = runtime(skill_name=name, input_envelope=child_envelope, child_run_id=run_id,
                      model=model, event_sink=child_sink)
        if inspect.isawaitable(raw):
            raw = await raw
        result = normalize_child_skill_result(raw, child_run_id=run_id, skill_name=name)
        trace_status = "ask_user" if result["paused_for_user"] else ("completed" if result["success"] else "failed")
        event_name = "skill_ask_user" if result["paused_for_user"] else ("skill_completed" if result["success"] else "skill_failed")
        trace = {"step_id": "skill_1", "skill_name": name, "child_run_id": run_id,
                 "status": trace_status}
        await _emit_parent_event(event_sink, {event_name: deepcopy(trace)})
        parent = {"success": result["success"], "completed": result["completed"],
                "paused_for_user": result["paused_for_user"], "mode": result["mode"] if result["paused_for_user"] else "single_skill",
                "skill_name": name, "child_run_id": run_id, "reason": result["reason"],
                "missing": result["missing"], "selected_skill_count": 1,
                "skill_results": {"skill_1": result},
                "skills": {"skill_1": {"skill_name": name, "child_run_id": run_id,
                                           "status": result["status"], "result": result}},
                "artifacts": deepcopy(result["artifacts"]),
                "output_files": deepcopy(result["output_files"]),
                "multi_skill_trace": [trace], "text": ""}
        if result["success"] is True and not result["paused_for_user"]:
            parent["text"] = await _synthesize_parent_result(user_request=user_request, parent=parent,
                final_synthesizer=final_synthesizer, model=model)
        return parent
    canonical = decision["plan"]
    if canonical["missing_required_inputs"]:
        return {"success": None, "completed": False, "paused_for_user": True,
                "mode": "ask_user", "reason": "Multi-Skill plan requires additional input.",
                "missing": deepcopy(canonical["missing_required_inputs"]), "plan": canonical}
    if execution_mode == "plan":
        return {"success": True, "mode": "plan", "selection_mode": "multi_skill", "plan": canonical,
                "preview": preview_multiskill_plan(canonical)}
    result = await execute_multiskill_plan(plan=canonical, activated_skill_names=names,
        parent_envelope=parent_envelope, single_skill_runtime=single_skill_runtime,
        event_sink=event_sink, model=model)
    if result.get("success") and result.get("mode") == "multi_skill":
        result["text"] = await _synthesize_parent_result(user_request=user_request, parent=result,
            final_synthesizer=final_synthesizer, model=model)
    return result


async def _emit_parent_event(event_sink, event: dict) -> None:
    if event_sink:
        value = event_sink(event)
        if inspect.isawaitable(value):
            await value


async def _synthesize_parent_result(*, user_request: str, parent: dict,
                                    final_synthesizer, model: str | None) -> str:
    synthesizer = final_synthesizer or synthesize_multiskill_result
    kwargs = dict(original_user_request=user_request,
        child_results=deepcopy(parent["skill_results"]), artifacts=deepcopy(parent["artifacts"]),
        output_files=deepcopy(parent["output_files"]))
    if synthesizer is synthesize_multiskill_result:
        kwargs["model"] = model
    value = synthesizer(**kwargs)
    if inspect.isawaitable(value):
        value = await value
    return str(value or "")


async def synthesize_multiskill_result(*, original_user_request: str, child_results: dict,
                                       artifacts: list, output_files: list, model: str | None = None) -> str:
    """Generate text only from normalized public manifests; execution truth is immutable."""
    messages = [{"role": "system", "content": (
        "Write the final answer for the user using only these public Child Result Manifests and artifact manifests. "
        "Do not invent execution outcomes or paths. Return user-facing text only."
    )}, {"role": "user", "content": json.dumps({
        "original_user_request": original_user_request, "child_results": child_results,
        "artifacts": artifacts, "output_files": output_files,
    }, ensure_ascii=False)}]
    return str(await complete_chat_once(messages, model or settings.default_model))


async def run_multiskill_orchestration(*, user_request: str, parent_envelope: dict,
                                       execution_mode="execute", confirmed_plan=None,
                                       single_skill_runtime=None, candidate_model_call=None,
                                       planner_model_call=None, final_synthesizer=None,
                                       event_sink=None, model: str | None = None) -> dict:
    """Production #465 discovery/activation to #466 composition entry point."""
    summary = {key: bool(value) for key, value in parent_envelope.items() if not key.startswith("_")}
    if confirmed_plan is not None:
        # Confirmation executes the same canonical plan and only refreshes its
        # governance-backed activation records; no discovery/planner rerun.
        activation_cards = build_multiskill_activation_cards([
            step.get("skill_name") for step in confirmed_plan.get("steps", []) if isinstance(step, dict)
        ])
    else:
        discovery_cards = build_multiskill_catalog(user_request=user_request, input_envelope_summary=summary)
        shortlist = await _plan_skill_candidates_with_model(user_request=user_request,
            input_envelope_summary=summary, discovery_cards=discovery_cards,
            model=model, model_call=candidate_model_call)
        activation_cards = build_multiskill_activation_cards([
            item["skill_name"] for item in shortlist["candidates"]
        ])
    if not activation_cards:
        return {"success": False, "completed": False, "mode": "no_skill",
                "reason": "No executable skill was activated."}
    result = await run_multiskill_manager(user_request=user_request, parent_envelope=parent_envelope,
        activation_cards=activation_cards, single_skill_runtime=single_skill_runtime,
        execution_mode=execution_mode, confirmed_plan=confirmed_plan, model_call=planner_model_call,
        event_sink=event_sink, final_synthesizer=final_synthesizer, model=model)
    if result.get("mode") == "plan":
        result["plan_id"] = _store_multiskill_plan(selection_mode=result.get("selection_mode") or "multi_skill",
            plan=result.get("plan"), skill_name=result.get("skill_name"), user_request=user_request,
            parent_envelope=parent_envelope, model=model)
    return result


async def confirm_multiskill_plan(plan_id: str, *, single_skill_runtime=None,
                                  event_sink=None, final_synthesizer=None) -> dict:
    """Consume a pending plan and execute it without discovery or planning."""
    pending = _take_multiskill_plan(plan_id)
    if pending.get("selection_mode", "multi_skill") == "single_skill":
        cards = build_multiskill_activation_cards([pending["skill_name"]])
        return await run_multiskill_manager(user_request=pending["user_request"],
            parent_envelope=pending["parent_envelope"], activation_cards=cards,
            execution_mode="execute", confirmed_skill_name=pending["skill_name"],
            single_skill_runtime=single_skill_runtime, event_sink=event_sink,
            final_synthesizer=final_synthesizer, model=pending.get("model"))
    return await run_multiskill_orchestration(
        user_request=pending["user_request"], parent_envelope=pending["parent_envelope"],
        execution_mode="execute", confirmed_plan=pending["plan"],
        single_skill_runtime=single_skill_runtime, event_sink=event_sink,
        final_synthesizer=final_synthesizer, model=pending.get("model"),
    )
