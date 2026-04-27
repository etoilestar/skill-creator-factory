"""Package a skill directory into a downloadable ZIP file."""
import os
import re
import zipfile
from pathlib import Path

from .config import SKILL_DATA_PATH

# Matches valid skill names: lowercase alphanum + hyphens, no leading/trailing hyphen
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _safe_skill_dir(skill_name: str) -> Path:
    """Build and confine the skill directory path.

    Validates skill_name format, then resolves the path and asserts it
    stays within SKILL_DATA_PATH to prevent path traversal.
    """
    if not _SKILL_NAME_RE.match(skill_name):
        raise ValueError(f"Invalid skill name: {skill_name!r}")
    base = Path(SKILL_DATA_PATH).resolve()
    # Use only the basename to strip any embedded separators
    safe_name = os.path.basename(skill_name)
    candidate = (base / safe_name).resolve()
    # Path confinement: reject anything that escapes SKILL_DATA_PATH
    if not str(candidate).startswith(str(base) + os.sep) and candidate != base:
        raise ValueError(f"Path traversal detected for skill name: {skill_name!r}")
    return candidate


def package_skill(skill_name: str) -> str:
    """Zip skill-data/{skill_name} and return the absolute path to the ZIP.

    Raises FileNotFoundError if skill directory doesn't exist.
    Raises ValueError if skill_name is invalid.
    """
    skill_dir = _safe_skill_dir(skill_name)
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")

    packages_dir = Path(SKILL_DATA_PATH) / ".packages"
    packages_dir.mkdir(parents=True, exist_ok=True)

    zip_path = packages_dir / f"{skill_dir.name}.zip"
    # Replace existing package
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in skill_dir.rglob("*"):
            if file.is_file():
                arcname = Path(skill_dir.name) / file.relative_to(skill_dir)
                zf.write(file, arcname)

    return str(zip_path.resolve())
