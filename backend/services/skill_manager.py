import re
import shutil
from pathlib import Path

import yaml

from ..config import settings


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md file."""
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if match:
        try:
            return yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return {}
    return {}


def _skill_info(skill_dir: Path) -> dict:
    skill_md = skill_dir / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    meta = _parse_frontmatter(content)
    return {
        "name": skill_dir.name,
        "display_name": meta.get("name", skill_dir.name),
        "description": meta.get("description", ""),
    }


def list_skills() -> list[dict]:
    if not settings.skills_path.exists():
        return []
    return [
        _skill_info(d)
        for d in sorted(settings.skills_path.iterdir())
        if d.is_dir() and (d / "SKILL.md").exists()
    ]


def get_skill(skill_name: str) -> dict:
    skill_dir = settings.skills_path / skill_name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    content = skill_md.read_text(encoding="utf-8")
    meta = _parse_frontmatter(content)
    return {
        "name": skill_dir.name,
        "display_name": meta.get("name", skill_dir.name),
        "description": meta.get("description", ""),
        "content": content,
    }


def save_skill(skill_name: str, content: str) -> dict:
    """Create or overwrite a skill's SKILL.md."""
    skill_dir = settings.skills_path / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    meta = _parse_frontmatter(content)
    return {
        "name": skill_dir.name,
        "display_name": meta.get("name", skill_dir.name),
        "description": meta.get("description", ""),
    }


def delete_skill(skill_name: str) -> None:
    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    shutil.rmtree(skill_dir)
