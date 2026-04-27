"""Package a skill directory into a downloadable ZIP file."""
import re
import zipfile
from pathlib import Path

from .config import SKILL_DATA_PATH

# Matches valid skill names: lowercase alphanum + hyphens, no leading/trailing hyphen.
# Must stay in sync with user_input_handler._SKILL_ID_RE.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _find_skill_dir(skill_name: str) -> Path:
    """Locate the skill directory via filesystem scan rather than path construction.

    First validates skill_name format (regex), then finds the matching entry by
    iterating SKILL_DATA_PATH.  The returned Path is filesystem-derived from
    iterdir(), not constructed from user-supplied characters, which prevents
    path traversal regardless of the input.

    Raises ValueError  if skill_name is invalid.
    Raises FileNotFoundError if no matching directory exists.
    """
    if not _SKILL_NAME_RE.match(skill_name):
        raise ValueError(f"Invalid skill name: {skill_name!r}")
    base = Path(SKILL_DATA_PATH).resolve()
    if base.is_dir():
        for entry in base.iterdir():
            if entry.is_dir() and entry.name == skill_name:
                return entry
    raise FileNotFoundError(f"Skill directory not found: {skill_name!r}")


def package_skill(skill_name: str) -> str:
    """Zip the skill directory and return the absolute path to the ZIP.

    Raises FileNotFoundError if skill directory doesn't exist.
    Raises ValueError if skill_name is invalid.
    """
    # skill_dir is filesystem-derived (from iterdir), not constructed from user input.
    skill_dir = _find_skill_dir(skill_name)

    packages_dir = Path(SKILL_DATA_PATH) / ".packages"
    packages_dir.mkdir(parents=True, exist_ok=True)

    # validated_name comes from the filesystem entry, not from the raw user string.
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
