"""High-level Multi-Skill decision, preview, execution, and safe synthesis."""

from __future__ import annotations

import inspect
import json
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
from .multiskill_executor import execute_multiskill_plan, normalize_child_skill_result
from .multiskill_plan import plan_multiskill, validate_multiskill_plan


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
                                 confirmed_plan: dict | None = None, model_call=None, event_sink=None,
                                 final_synthesizer: Callable | None = None, model: str | None = None) -> dict:
    """Run one decision. Confirmed plans are validated and executed without replanning."""
    names = [card["skill_name"] for card in activation_cards]
    if confirmed_plan is not None:
        decision = {"mode": "multi_skill", "plan": validate_multiskill_plan(confirmed_plan, names)}
    else:
        decision = await plan_multiskill(user_request=user_request,
            input_envelope_summary={key: bool(value) for key, value in parent_envelope.items()},
            activation_cards=activation_cards, model_call=model_call)
    if decision["mode"] == "single_skill":
        name = decision["skill_name"]
        run_id = f"child_{uuid.uuid4().hex}"
        runtime = single_skill_runtime or execute_child_skill_runtime
        raw = runtime(skill_name=name, input_envelope=deepcopy(parent_envelope), child_run_id=run_id)
        if inspect.isawaitable(raw):
            raw = await raw
        result = normalize_child_skill_result(raw, child_run_id=run_id, skill_name=name)
        return {"success": result["success"], "completed": result["completed"],
                "paused_for_user": result["paused_for_user"], "mode": result["mode"] if result["paused_for_user"] else "single_skill",
                "skill_name": name, "child_run_id": run_id, "reason": result["reason"],
                "missing": result["missing"], "result": result}
    canonical = decision["plan"]
    if execution_mode == "plan":
        return {"success": True, "mode": "plan", "plan": canonical,
                "preview": preview_multiskill_plan(canonical)}
    result = await execute_multiskill_plan(plan=canonical, activated_skill_names=names,
        parent_envelope=parent_envelope, single_skill_runtime=single_skill_runtime, event_sink=event_sink)
    if result.get("success") and result.get("mode") == "multi_skill":
        synthesizer = final_synthesizer or synthesize_multiskill_result
        synthesis_kwargs = dict(original_user_request=user_request,
            child_results=deepcopy(result["skill_results"]), artifacts=deepcopy(result["artifacts"]),
            output_files=deepcopy(result["output_files"]))
        if synthesizer is synthesize_multiskill_result:
            synthesis_kwargs["model"] = model
        synthesis = synthesizer(**synthesis_kwargs)
        if inspect.isawaitable(synthesis):
            synthesis = await synthesis
        result["text"] = str(synthesis or "")
    return result


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
    summary = {key: bool(value) for key, value in parent_envelope.items()}
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
    return await run_multiskill_manager(user_request=user_request, parent_envelope=parent_envelope,
        activation_cards=activation_cards, single_skill_runtime=single_skill_runtime,
        execution_mode=execution_mode, confirmed_plan=confirmed_plan, model_call=planner_model_call,
        event_sink=event_sink, final_synthesizer=final_synthesizer, model=model)
