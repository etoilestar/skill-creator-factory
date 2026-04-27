"""Package a skill directory into a downloadable ZIP file."""
import re
import zipfile
from pathlib import Path

from .config import SKILL_DATA_PATH

# Matches valid skill names: lowercase alphanum + hyphens, no leading/trailing hyphen.
# Must stay in sync with user_input_handler._SKILL_ID_RE.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _safe_skill_dir(skill_name: str) -> Path:
    """Validate skill_name and return a confined, resolved skill directory path.

    After regex validation the name contains only [a-z0-9-] characters, so it
    cannot carry path separators or traversal sequences.  Resolving the path
    and checking is_relative_to() provides a second layer of defense.
    """
    if not _SKILL_NAME_RE.match(skill_name):
        raise ValueError(f"Invalid skill name: {skill_name!r}")
    base = Path(SKILL_DATA_PATH).resolve()
    candidate = (base / skill_name).resolve()
    if not candidate.is_relative_to(base):
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

    # Use the filesystem-resolved name (not the raw user input) for the zip filename.
    validated_name = skill_dir.name
    zip_path = packages_dir / f"{validated_name}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in skill_dir.rglob("*"):
            if file.is_file():
                arcname = Path(validated_name) / file.relative_to(skill_dir)
                zf.write(file, arcname)

    return str(zip_path.resolve())
