"""High-level Multi-Skill decision, preview, execution, and safe synthesis."""

from __future__ import annotations

import inspect
from copy import deepcopy
from typing import Callable

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
                                 single_skill_runtime: Callable, execution_mode="execute",
                                 confirmed_plan: dict | None = None, model_call=None, event_sink=None,
                                 final_synthesizer: Callable | None = None) -> dict:
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
        raw = single_skill_runtime(skill_name=name, input_envelope=deepcopy(parent_envelope))
        if inspect.isawaitable(raw):
            raw = await raw
        return {"success": raw.get("success", True), "mode": "single_skill", "skill_name": name, "result": raw}
    canonical = decision["plan"]
    if execution_mode == "plan":
        return {"success": True, "mode": "plan", "plan": canonical,
                "preview": preview_multiskill_plan(canonical)}
    result = await execute_multiskill_plan(plan=canonical, activated_skill_names=names,
        parent_envelope=parent_envelope, single_skill_runtime=single_skill_runtime, event_sink=event_sink)
    if result.get("success") and result.get("mode") == "multi_skill" and final_synthesizer:
        synthesis = final_synthesizer(original_user_request=user_request,
            child_results=deepcopy(result["skill_results"]), artifacts=deepcopy(result["artifacts"]),
            output_files=deepcopy(result["output_files"]))
        if inspect.isawaitable(synthesis):
            synthesis = await synthesis
        result["text"] = str(synthesis or "")
    return result
