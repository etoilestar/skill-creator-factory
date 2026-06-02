"""Publish skill prompt injection service.

Builds system prompts from enabled skills for published endpoints.
Reads SKILL.md files and composes them into a unified system prompt.
"""

import logging
from pathlib import Path

from .skill_governance import resolve_skill_record
from .kernel_loader import load_skill_body_prompt

logger = logging.getLogger(__name__)


def build_system_prompt(enabled_skills: list[str]) -> str:
    """Build a composite system prompt from all enabled skills.

    Reads each skill's SKILL.md body and combines them into a single
    system prompt suitable for injection into chat completions.
    """
    if not enabled_skills:
        return ""

    parts: list[str] = []
    parts.append("You are an AI assistant with the following specialized skills:\n")

    for skill_name in enabled_skills:
        try:
            body = load_skill_body_prompt(skill_name)
            if body:
                parts.append(f"\n--- Skill: {skill_name} ---\n")
                parts.append(body)
        except (FileNotFoundError, PermissionError) as exc:
            logger.warning(
                "[Publish] Failed to load skill '%s': %s", skill_name, exc
            )
            continue

    if len(parts) <= 1:
        return ""

    parts.append(
        "\n\n--- Instructions ---\n"
        "Use the above skills to assist the user. "
        "Apply the relevant skill based on the user's request."
    )

    return "\n".join(parts)


def get_skill_descriptions(enabled_skills: list[str]) -> str:
    """Get a brief description summary of enabled skills.

    Used for model metadata in /v1/models responses.
    """
    if not enabled_skills:
        return "General-purpose AI assistant"

    descriptions: list[str] = []
    for skill_name in enabled_skills:
        try:
            record = resolve_skill_record(skill_name, mode="manage")
            desc = record.get("description", skill_name)
            descriptions.append(f"{skill_name}: {desc}" if desc else skill_name)
        except (FileNotFoundError, PermissionError):
            descriptions.append(skill_name)

    return f"AI assistant with skills: {', '.join(descriptions)}"
