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


_ALLOWED_ASSET_FOLDERS = {"scripts", "references", "assets"}
_MAX_ASSET_BYTES = 10 * 1024 * 1024  # 10 MB


def save_asset(skill_name: str, folder: str, filename: str, data: bytes) -> dict:
    """Save an uploaded file to a skill sub-directory.

    Raises:
        FileNotFoundError: if the skill does not exist.
        ValueError: if folder or filename is invalid, or data exceeds size limit.
    """
    if folder not in _ALLOWED_ASSET_FOLDERS:
        raise ValueError(f"folder must be one of {sorted(_ALLOWED_ASSET_FOLDERS)}")
    safe_name = Path(filename).name
    if (
        not safe_name
        or safe_name.startswith(".")
        or "\x00" in safe_name
        or "/" in safe_name
        or "\\" in safe_name
        or len(safe_name) > 255
    ):
        raise ValueError("Invalid filename")
    if len(data) > _MAX_ASSET_BYTES:
        raise ValueError("File exceeds 10 MB limit")
    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    target_dir = skill_dir / folder
    target_dir.mkdir(exist_ok=True)
    dest = target_dir / safe_name
    dest.write_bytes(data)
    return {
        "skill": skill_name,
        "folder": folder,
        "filename": safe_name,
        "path": str(dest.relative_to(settings.skills_path.parent)),
        "size": len(data),
    }


def list_skill_assets(skill_name: str) -> dict:
    """Return filenames grouped by sub-directory for a skill.

    Raises:
        FileNotFoundError: if the skill does not exist.
    """
    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    result: dict[str, list[str]] = {}
    for folder in sorted(_ALLOWED_ASSET_FOLDERS):
        folder_dir = skill_dir / folder
        if folder_dir.is_dir():
            result[folder] = sorted(p.name for p in folder_dir.iterdir() if p.is_file())
        else:
            result[folder] = []
    return result


def delete_asset(skill_name: str, folder: str, filename: str) -> None:
    """Delete a single asset file from a skill sub-directory.

    Raises:
        FileNotFoundError: if the skill or file does not exist.
        ValueError: if folder or filename is invalid.
    """
    if folder not in _ALLOWED_ASSET_FOLDERS:
        raise ValueError(f"folder must be one of {sorted(_ALLOWED_ASSET_FOLDERS)}")
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith(".") or "\x00" in safe_name or "/" in safe_name or "\\" in safe_name or len(safe_name) > 255:
        raise ValueError("Invalid filename")
    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    target = skill_dir / folder / safe_name
    if not target.is_file():
        raise FileNotFoundError(f"Asset '{safe_name}' not found in '{folder}'")
    target.unlink()
