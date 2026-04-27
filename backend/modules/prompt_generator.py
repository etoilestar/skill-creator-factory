"""Generate structured prompts based on current session step.

No LLM calls — pure string assembly from kernel template data.
"""
from . import skill_kernel_loader, state_machine

_steps_cache: list[dict] | None = None


def _get_all_steps() -> list[tuple[int, int, dict]]:
    """Flatten phases into (phase, step_index, step_data) tuples."""
    global _steps_cache
    if _steps_cache is None:
        _steps_cache = []
        for phase_data in skill_kernel_loader.get_skill_steps():
            for idx, step in enumerate(phase_data["steps"]):
                _steps_cache.append((phase_data["phase"], idx, step))
    return _steps_cache


def generate_prompt(session_id: str) -> dict:
    """Return the current question + options for a session.
    
    Returns:
        {
            "question": str,
            "options": list[str],
            "step": str,      # step id
            "phase": int,
            "phase_label": str,
            "step_index": int,
            "total_steps": int,
            "completed": bool,
        }
    """
    current = state_machine.get_current_step(session_id)
    if current["completed"]:
        return {
            "question": "所有步骤已完成！请点击\u201c生成技能\u201d按钮。",
            "options": [],
            "step": "done",
            "phase": current["phase"],
            "phase_label": "完成",
            "step_index": 0,
            "total_steps": 0,
            "completed": True,
        }

    phases = skill_kernel_loader.get_skill_steps()
    phase_data = next((p for p in phases if p["phase"] == current["phase"]), None)
    if phase_data is None:
        return {"question": "未知步骤", "options": [], "step": "unknown",
                "phase": current["phase"], "phase_label": "", "step_index": 0,
                "total_steps": 0, "completed": False}

    steps = phase_data["steps"]
    step_index = current["step_index"]
    if step_index >= len(steps):
        step_index = len(steps) - 1
    step = steps[step_index]

    return {
        "question": step["question"],
        "options": step.get("options", []),
        "step": step["id"],
        "phase": current["phase"],
        "phase_label": phase_data["label"],
        "step_index": step_index,
        "total_steps": len(steps),
        "completed": False,
    }
