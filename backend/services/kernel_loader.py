from pathlib import Path

from ..config import settings


def load_kernel_system_prompt() -> str:
    """Load kernel/SKILL.md as the skill-creator system prompt."""
    skill_md = settings.kernel_path / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Kernel SKILL.md not found at {skill_md}")
    return skill_md.read_text(encoding="utf-8")


def load_skill_system_prompt(skill_name: str) -> str:
    """Load a user skill's SKILL.md as the sandbox system prompt."""
    skill_md = settings.skills_path / skill_name / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    return skill_md.read_text(encoding="utf-8")
