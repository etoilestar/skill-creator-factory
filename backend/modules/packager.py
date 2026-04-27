"""Package a skill directory into a downloadable ZIP file."""
import zipfile
from pathlib import Path

from .config import SKILL_DATA_PATH


def package_skill(skill_name: str) -> str:
    """Zip skill-data/{skill_name} and return the absolute path to the ZIP.
    
    Raises FileNotFoundError if skill directory doesn't exist.
    """
    skill_dir = Path(SKILL_DATA_PATH) / skill_name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")

    packages_dir = Path(SKILL_DATA_PATH) / ".packages"
    packages_dir.mkdir(parents=True, exist_ok=True)

    zip_path = packages_dir / f"{skill_name}.zip"
    # Replace existing package
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in skill_dir.rglob("*"):
            if file.is_file():
                arcname = Path(skill_name) / file.relative_to(skill_dir)
                zf.write(file, arcname)

    return str(zip_path.resolve())
