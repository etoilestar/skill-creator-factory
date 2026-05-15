import io
import shutil
import zipfile
from pathlib import Path

from ..config import settings
from .skill_metadata import parse_skill_frontmatter
from .skill_governance import (
    get_scope_skill_record,
    list_skills_for_mode,
    log_access_decision,
    managed_skill_root,
    record_installation,
    resolve_skill_record,
    rollback_skill as governance_rollback_skill,
    skill_versions,
)


def _resolved_skill_dir(skill_name: str, *, mode: str = "manage", require_executable: bool = False) -> Path:
    record = resolve_skill_record(
        skill_name,
        mode=mode,
        require_visible=True,
        require_executable=require_executable,
    )
    return Path(record["root_path"])


def _managed_skill_dir(skill_name: str) -> Path:
    return managed_skill_root(skill_name)


def _managed_root_parent() -> Path:
    return Path(getattr(settings, "skills_path", settings.managed_skills_path)).parent


def _resolve_version(meta: dict, *, existing_version: str | None = None) -> str:
    return str(meta.get("version") or existing_version or "0.1.0")


def _skill_info(record: dict) -> dict:
    return {
        "skill_id": record["skill_id"],
        "name": record["name"],
        "display_name": record.get("display_name", record["name"]),
        "description": record.get("description", ""),
        "version": record.get("version"),
        "source": record.get("source"),
        "install_type": record.get("install_type"),
        "scope": record.get("scope"),
        "resolved_scope": record.get("resolved_scope", record.get("scope")),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "approval_requested_at": record.get("approval_requested_at"),
        "available_scopes": record.get("available_scopes", [record.get("scope")]),
        "shadowed_scopes": record.get("shadowed_scopes", []),
        "editable": record.get("editable", False),
        "can_view": record.get("can_view", True),
        "can_execute": record.get("can_execute", False),
        "governance": record.get("governance", {}),
        "version_history": record.get("version_history", []),
        "install_history": record.get("install_history", []),
    }


def list_skills(mode: str = "manage", *, include_hidden: bool = False) -> list[dict]:
    return [_skill_info(record) for record in list_skills_for_mode(mode, include_hidden=include_hidden)]


def get_skill(skill_name: str, mode: str = "manage") -> dict:
    record = resolve_skill_record(skill_name, mode=mode, require_visible=True)
    skill_dir = Path(record["root_path"])
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    content = skill_md.read_text(encoding="utf-8")
    result = _skill_info(record)
    result["content"] = content
    return result


def save_skill(skill_name: str, content: str) -> dict:
    skill_dir = _managed_skill_dir(skill_name)
    existed = skill_dir.exists()
    previous_version = None
    if existed:
        try:
            previous_version = get_scope_skill_record(skill_name, "managed").get("version")
        except FileNotFoundError:
            previous_version = None
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    meta = parse_skill_frontmatter(content)
    version = _resolve_version(meta, existing_version=previous_version)
    status = "pending_review" if existed else "draft"
    result = record_installation(
        skill_name=skill_name,
        scope="managed",
        root_path=skill_dir,
        source={"type": "local", "origin": str(skill_dir / "SKILL.md")},
        install_type="manual_save",
        status=status,
        version=version,
        event="save" if existed else "create",
        approval_requested=existed,
        extra={"created": not existed},
    )
    return _skill_info(result)


def delete_skill(skill_name: str) -> None:
    skill_dir = _managed_skill_dir(skill_name)
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    shutil.rmtree(skill_dir)


_ALLOWED_ASSET_FOLDERS = {"scripts", "references", "assets"}
_MAX_ASSET_BYTES = 10 * 1024 * 1024


def save_asset(skill_name: str, folder: str, filename: str, data: bytes) -> dict:
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
    skill_dir = _managed_skill_dir(skill_name)
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
        "path": str(dest.relative_to(_managed_root_parent())),
        "size": len(data),
    }


def list_skill_assets(skill_name: str) -> dict:
    skill_dir = _resolved_skill_dir(skill_name, mode="manage")
    result: dict[str, list[str]] = {}
    for folder in sorted(_ALLOWED_ASSET_FOLDERS):
        folder_dir = skill_dir / folder
        result[folder] = sorted(p.name for p in folder_dir.iterdir() if p.is_file()) if folder_dir.is_dir() else []
    return result


def get_asset(skill_name: str, folder: str, filename: str) -> str:
    if folder not in _ALLOWED_ASSET_FOLDERS:
        raise ValueError(f"folder must be one of {sorted(_ALLOWED_ASSET_FOLDERS)}")
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith(".") or "\x00" in safe_name or "/" in safe_name or "\\" in safe_name or len(safe_name) > 255:
        raise ValueError("Invalid filename")
    skill_dir = _resolved_skill_dir(skill_name, mode="manage")
    target = skill_dir / folder / safe_name
    if not target.is_file():
        raise FileNotFoundError(f"Asset '{safe_name}' not found in '{folder}'")
    raw = target.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Binary files cannot be edited") from exc


def update_asset(skill_name: str, folder: str, filename: str, content: str) -> dict:
    if folder not in _ALLOWED_ASSET_FOLDERS:
        raise ValueError(f"folder must be one of {sorted(_ALLOWED_ASSET_FOLDERS)}")
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith(".") or "\x00" in safe_name or "/" in safe_name or "\\" in safe_name or len(safe_name) > 255:
        raise ValueError("Invalid filename")
    skill_dir = _managed_skill_dir(skill_name)
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
        "path": str(target.relative_to(_managed_root_parent())),
        "size": len(data),
    }


_MAX_ZIP_BYTES = 50 * 1024 * 1024
_MAX_UNZIP_BYTES = 50 * 1024 * 1024
_ALLOWED_ZIP_SUBDIRS = {"scripts", "references", "assets"}


def _parse_zip_payload(data: bytes) -> tuple[str, dict, str, list[tuple[str, bytes]], dict]:
    if len(data) > _MAX_ZIP_BYTES:
        raise ValueError("ZIP 文件超过 50 MB 限制")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是合法的 ZIP 格式") from exc

    with zf:
        names = zf.namelist()
        for entry in names:
            parts = Path(entry).parts
            if any(path_component in ("..", "") or path_component.startswith("/") for path_component in parts):
                raise ValueError(f"ZIP 包含非法路径: {entry}")

        real_names = [name for name in names if not name.startswith("__MACOSX/") and name != "__MACOSX"]
        skill_md_candidates = [
            name for name in real_names if name == "SKILL.md" or (name.endswith("/SKILL.md") and name.count("/") == 1)
        ]
        if not skill_md_candidates:
            raise ValueError("ZIP 中缺少 SKILL.md 文件")

        skill_md_path = skill_md_candidates[0]
        prefix = skill_md_path[: -len("SKILL.md")]
        skill_md_content = zf.read(skill_md_path).decode("utf-8")
        meta = parse_skill_frontmatter(skill_md_content)

        skill_name = meta.get("name", "").strip() or prefix.rstrip("/")
        if not skill_name:
            raise ValueError("无法确定 Skill 名称：请在 SKILL.md 的 frontmatter 中设置 name 字段")

        safe_skill_name = Path(skill_name).name
        if not safe_skill_name or safe_skill_name != skill_name or "\x00" in safe_skill_name:
            raise ValueError(f"SKILL.md 中的 name 字段包含非法字符: {skill_name!r}")

        total_size = sum(info.file_size for info in zf.infolist())
        if total_size > _MAX_UNZIP_BYTES:
            raise ValueError("ZIP 解压后内容超过 50 MB 限制")

        entries_to_extract: list[tuple[str, bytes]] = []
        for info in zf.infolist():
            entry_name = info.filename
            if entry_name.startswith("__MACOSX/") or entry_name == "__MACOSX":
                continue
            rel = entry_name[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            rel_parts = Path(rel).parts
            if len(rel_parts) == 1:
                if rel_parts[0] != "SKILL.md":
                    continue
            elif rel_parts[0] not in _ALLOWED_ZIP_SUBDIRS:
                continue
            fname = rel_parts[-1]
            if fname.startswith(".") or "\x00" in fname or len(fname) > 255:
                continue
            entries_to_extract.append((rel, zf.read(info.filename)))

        return safe_skill_name, meta, skill_md_content, entries_to_extract, {
            "archive_entries": len(entries_to_extract),
            "zip_size": len(data),
        }


def import_skill_zip(data: bytes, overwrite: bool = False) -> dict:
    skill_name, meta, _content, entries_to_extract, install_details = _parse_zip_payload(data)
    skill_dir = _managed_skill_dir(skill_name)
    exists = skill_dir.exists()
    if exists and not overwrite:
        raise FileExistsError(skill_name)

    if exists:
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in entries_to_extract:
        dest = skill_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    existing_version = get_scope_skill_record(skill_name, "managed").get("version") if exists else None
    version = _resolve_version(meta, existing_version=existing_version)
    result = record_installation(
        skill_name=skill_name,
        scope="managed",
        root_path=skill_dir,
        source={"type": "zip", "origin": "upload"},
        install_type="zip_import" if not exists else "zip_upgrade",
        status="pending_review",
        version=version,
        event="install" if not exists else "upgrade",
        approval_requested=True,
        extra={
            "overwrite": overwrite,
            **install_details,
        },
    )
    response = _skill_info(result)
    response["installation"] = result["install_history"][-1]
    return response


def upgrade_skill_zip(skill_name: str, data: bytes) -> dict:
    parsed_skill_name, _meta, _content, _entries, _details = _parse_zip_payload(data)
    if parsed_skill_name != skill_name:
        raise ValueError(f"ZIP 中的 skill 名称为 '{parsed_skill_name}'，与目标 '{skill_name}' 不一致")
    return import_skill_zip(data, overwrite=True)


def delete_asset(skill_name: str, folder: str, filename: str) -> None:
    if folder not in _ALLOWED_ASSET_FOLDERS:
        raise ValueError(f"folder must be one of {sorted(_ALLOWED_ASSET_FOLDERS)}")
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith(".") or "\x00" in safe_name or "/" in safe_name or "\\" in safe_name or len(safe_name) > 255:
        raise ValueError("Invalid filename")
    skill_dir = _managed_skill_dir(skill_name)
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    target = skill_dir / folder / safe_name
    if not target.is_file():
        raise FileNotFoundError(f"Asset '{safe_name}' not found in '{folder}'")
    target.unlink()


def get_execution_skill_dir(skill_name: str, *, mode: str = "sandbox") -> Path:
    try:
        record = resolve_skill_record(skill_name, mode=mode, require_visible=True, require_executable=True)
        log_access_decision(skill_name, record["scope"], mode=mode, action="execute", allowed=True)
        return Path(record["root_path"])
    except PermissionError as exc:
        try:
            record = resolve_skill_record(skill_name, mode=mode, require_visible=False, require_executable=False)
            log_access_decision(skill_name, record["scope"], mode=mode, action="execute", allowed=False, reason=str(exc))
        except FileNotFoundError:
            pass
        raise


def get_visible_skill_dir(skill_name: str, *, mode: str = "manage") -> Path:
    try:
        record = resolve_skill_record(skill_name, mode=mode, require_visible=True, require_executable=False)
        log_access_decision(skill_name, record["scope"], mode=mode, action="read", allowed=True)
        return Path(record["root_path"])
    except PermissionError as exc:
        try:
            record = resolve_skill_record(skill_name, mode=mode, require_visible=False, require_executable=False)
            log_access_decision(skill_name, record["scope"], mode=mode, action="read", allowed=False, reason=str(exc))
        except FileNotFoundError:
            pass
        raise


def rollback_skill(skill_name: str, version: str) -> dict:
    return _skill_info(governance_rollback_skill(skill_name, version))


def get_skill_versions(skill_name: str) -> dict:
    return skill_versions(skill_name)
