import io
import re
import shutil
import zipfile
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


def get_asset(skill_name: str, folder: str, filename: str) -> str:
    """Read a text asset file and return its content as a string.

    Raises:
        FileNotFoundError: if the skill or file does not exist.
        ValueError: if folder or filename is invalid, or the file is binary.
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
    raw = target.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Binary files cannot be edited") from exc


def update_asset(skill_name: str, folder: str, filename: str, content: str) -> dict:
    """Overwrite a text asset file with new content.

    Raises:
        FileNotFoundError: if the skill or file does not exist.
        ValueError: if folder or filename is invalid, or content exceeds size limit.
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
    data = content.encode("utf-8")
    if len(data) > _MAX_ASSET_BYTES:
        raise ValueError("File exceeds 10 MB limit")
    target.write_bytes(data)
    return {
        "skill": skill_name,
        "folder": folder,
        "filename": safe_name,
        "path": str(target.relative_to(settings.skills_path.parent)),
        "size": len(data),
    }


_MAX_ZIP_BYTES = 50 * 1024 * 1024       # 50 MB raw zip
_MAX_UNZIP_BYTES = 50 * 1024 * 1024    # 50 MB total extracted

# Allowed top-level sub-directories inside a skill zip
_ALLOWED_ZIP_SUBDIRS = {"scripts", "references", "assets"}


def import_skill_zip(data: bytes, overwrite: bool = False) -> dict:
    """Import a skill from a .zip file downloaded from skillsmp.

    The zip may contain either:
    - a top-level directory with SKILL.md inside  (my-skill/SKILL.md)
    - or SKILL.md at the zip root                 (SKILL.md)

    The skill directory name is derived (in order of priority) from:
    1. The ``name`` field in SKILL.md frontmatter
    2. The top-level directory name inside the zip

    Raises:
        ValueError: malformed zip, missing SKILL.md, unsafe paths, or oversized.
        FileExistsError: skill already exists and overwrite is False.
    """
    if len(data) > _MAX_ZIP_BYTES:
        raise ValueError("ZIP 文件超过 50 MB 限制")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是合法的 ZIP 格式") from exc

    with zf:
        names = zf.namelist()

        # Security: reject path traversal in any entry
        for entry in names:
            parts = Path(entry).parts
            if any(p in ("..", "") or p.startswith("/") for p in parts):
                raise ValueError(f"ZIP 包含非法路径: {entry}")

        # Detect structure: rooted (top-level dir) vs flat (SKILL.md at root)
        # Determine common prefix directory (if any)
        prefix = ""
        if names:
            first = names[0]
            candidate = first.split("/")[0] if "/" in first else ""
            if candidate and all(n.startswith(candidate + "/") for n in names):
                prefix = candidate + "/"

        skill_md_path = prefix + "SKILL.md"
        if skill_md_path not in names:
            raise ValueError("ZIP 中缺少 SKILL.md 文件")

        # Read SKILL.md content
        skill_md_content = zf.read(skill_md_path).decode("utf-8")

        # Determine skill name from frontmatter, fallback to prefix dir name
        meta = _parse_frontmatter(skill_md_content)
        skill_name = meta.get("name", "").strip()
        if not skill_name and prefix:
            skill_name = prefix.rstrip("/")
        if not skill_name:
            raise ValueError("无法确定 Skill 名称：请在 SKILL.md 的 frontmatter 中设置 name 字段")

        # Validate skill name: must be a plain filename with no path components
        safe_skill_name = Path(skill_name).name
        if not safe_skill_name or safe_skill_name != skill_name or "\x00" in safe_skill_name:
            raise ValueError(f"SKILL.md 中的 name 字段包含非法字符: {skill_name!r}")

        skill_dir = settings.skills_path / safe_skill_name
        if skill_dir.exists() and not overwrite:
            raise FileExistsError(safe_skill_name)

        # Validate total uncompressed size (zip bomb protection)
        total_size = sum(info.file_size for info in zf.infolist())
        if total_size > _MAX_UNZIP_BYTES:
            raise ValueError("ZIP 解压后内容超过 50 MB 限制")

        # Collect entries to extract: SKILL.md and allowed sub-directories only
        entries_to_extract: list[zipfile.ZipInfo] = []
        for info in zf.infolist():
            entry_name = info.filename
            # Strip prefix to get relative path inside the skill
            rel = entry_name[len(prefix):]
            if not rel or rel.endswith("/"):
                continue  # skip directories

            # Validate relative path parts
            rel_parts = Path(rel).parts
            if len(rel_parts) == 1:
                # Top-level file: only SKILL.md is allowed
                if rel_parts[0] != "SKILL.md":
                    continue
            elif len(rel_parts) >= 2:
                # Sub-directory file: only allowed sub-dirs
                if rel_parts[0] not in _ALLOWED_ZIP_SUBDIRS:
                    continue
            else:
                continue

            # Filename safety check
            fname = rel_parts[-1]
            if fname.startswith(".") or "\x00" in fname or len(fname) > 255:
                continue

            entries_to_extract.append(info)

        # Write files
        if skill_dir.exists() and overwrite:
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)

        for info in entries_to_extract:
            rel = info.filename[len(prefix):]
            dest = skill_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info.filename))

    return {
        "name": safe_skill_name,
        "display_name": meta.get("name", safe_skill_name),
        "description": meta.get("description", ""),
    }


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
