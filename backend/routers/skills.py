import asyncio
import subprocess
import sys as _sys
from pathlib import Path as _Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services.skill_manager import (
    delete_asset,
    delete_skill,
    get_asset,
    get_skill,
    import_skill_zip,
    list_skill_assets,
    list_skills,
    save_asset,
    save_skill,
    update_asset,
)
from ..config import settings

router = APIRouter(prefix="/api/skills", tags=["skills"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_ZIP_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class SaveSkillRequest(BaseModel):
    name: str
    content: str


@router.get("")
async def get_all_skills():
    """List all skills in the skills directory."""
    return list_skills()


@router.post("/import")
async def import_skill_from_zip(
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
):
    """Import a skill from a .zip file (e.g. downloaded from skillsmp).

    Returns 409 Conflict when the skill already exists and overwrite is False.
    """
    if file.size is not None and file.size > _MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP 文件超过 50 MB 限制")
    data = await file.read()
    if len(data) > _MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP 文件超过 50 MB 限制")
    try:
        return import_skill_zip(data, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail={"message": f"Skill '{exc}' 已存在", "skill_name": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{skill_name}")
async def get_one_skill(skill_name: str):
    """Get a single skill's metadata and SKILL.md content."""
    try:
        return get_skill(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("")
async def create_or_update_skill(request: SaveSkillRequest):
    """Create or overwrite a skill with provided SKILL.md content."""
    return save_skill(request.name, request.content)


@router.delete("/{skill_name}")
async def remove_skill(skill_name: str):
    """Delete a skill directory."""
    try:
        delete_skill(skill_name)
        return {"message": f"Skill '{skill_name}' deleted"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{skill_name}/assets")
async def get_skill_assets(skill_name: str):
    """List asset files grouped by sub-directory for a skill."""
    try:
        return list_skill_assets(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{skill_name}/assets")
async def upload_skill_asset(
    skill_name: str,
    file: UploadFile = File(...),
    folder: str = Form("assets"),
):
    """Upload a reference file to a skill sub-directory (max 10 MB)."""
    # Reject oversized files before reading body when Content-Length is available
    if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    try:
        return save_asset(skill_name, folder, file.filename or "", data)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{skill_name}/assets/{folder}/{filename}")
async def get_skill_asset_content(skill_name: str, folder: str, filename: str):
    """Read the text content of a single asset file."""
    try:
        content = get_asset(skill_name, folder, filename)
        return {"content": content}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        if "Binary" in str(exc):
            raise HTTPException(status_code=415, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


class UpdateAssetRequest(BaseModel):
    content: str


@router.put("/{skill_name}/assets/{folder}/{filename}")
async def update_skill_asset_content(skill_name: str, folder: str, filename: str, request: UpdateAssetRequest):
    """Overwrite the text content of a single asset file."""
    try:
        return update_asset(skill_name, folder, filename, request.content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{skill_name}/assets/{folder}/{filename}")
async def remove_skill_asset(skill_name: str, folder: str, filename: str):
    """Delete a single asset file from a skill sub-directory."""
    try:
        delete_asset(skill_name, folder, filename)
        return {"message": f"Deleted '{filename}' from '{folder}'"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Script execution endpoint
# ---------------------------------------------------------------------------

_SCRIPT_RUN_TIMEOUT = 30  # seconds


class RunScriptRequest(BaseModel):
    args: list[str] = []
    stdin: str = ""


class RunScriptResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


@router.post("/{skill_name}/scripts/{filename}/run", response_model=RunScriptResponse)
async def run_skill_script(skill_name: str, filename: str, request: RunScriptRequest):
    """Execute a Python script from a skill's scripts/ directory.

    Restricted to scripts that live under skills/{skill_name}/scripts/.
    Output is capped at 100 KB each for stdout and stderr.
    Execution is limited to 30 seconds.
    """
    from pathlib import Path as _Path

    # Validate filename (no path traversal)
    safe_name = _Path(filename).name
    if (
        not safe_name
        or safe_name.startswith(".")
        or "\x00" in safe_name
        or len(safe_name) > 255
        or not safe_name.endswith(".py")
    ):
        raise HTTPException(status_code=400, detail="文件名非法或不是 .py 文件")

    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    script_path = skill_dir / "scripts" / safe_name
    if not script_path.is_file():
        raise HTTPException(status_code=404, detail=f"脚本 '{safe_name}' 不存在")

    # Validate extra args: no shell injection (no shell=True, but sanitise list)
    for arg in request.args:
        if "\x00" in arg:
            raise HTTPException(status_code=400, detail="参数包含非法字符")

    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable,
            str(script_path),
            *request.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(skill_dir / "scripts"),
        )
        _MAX_OUTPUT = 100 * 1024  # 100 KB
        stdin_bytes = request.stdin.encode("utf-8") if request.stdin else None
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=_SCRIPT_RUN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise HTTPException(status_code=408, detail=f"脚本执行超时（超过 {_SCRIPT_RUN_TIMEOUT} 秒）")

        return RunScriptResponse(
            stdout=stdout_bytes[:_MAX_OUTPUT].decode("utf-8", errors="replace"),
            stderr=stderr_bytes[:_MAX_OUTPUT].decode("utf-8", errors="replace"),
            exit_code=proc.returncode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"脚本执行失败: {exc}") from exc


@router.get("/{skill_name}/outputs")
async def list_skill_outputs(skill_name: str):
    """List files generated by skill scripts in the outputs/ directory.

    Returns name, relative path, download URL, size (bytes), and last-modified
    timestamp (Unix epoch seconds) for each file.
    """
    skill_dir = (settings.skills_path / skill_name).resolve()
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    outputs_dir = skill_dir / "outputs"
    if not outputs_dir.exists():
        return {"files": []}

    files = []
    for f in sorted(outputs_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(skill_dir)
        stat = f.stat()
        files.append({
            "name": f.name,
            "path": rel.as_posix(),
            "url": f"/api/skills/{skill_name}/files/{rel.as_posix()}",
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })
    return {"files": files}


@router.get("/{skill_name}/files/{filepath:path}")
async def download_skill_file(skill_name: str, filepath: str):
    """Download a file generated by a skill script.

    Serves any file that lives under skills/{skill_name}/, with path-traversal
    protection.  Typical use: output files written to skills/{name}/outputs/.
    """
    skill_dir = (settings.skills_path / skill_name).resolve()
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    # Reject obviously malicious inputs before constructing the path
    if "\x00" in filepath:
        raise HTTPException(status_code=400, detail="非法文件路径")

    target = (skill_dir / filepath).resolve()

    # Path-traversal guard: resolved path must stay inside the skill directory
    try:
        target.relative_to(skill_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="路径越界")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        path=str(target),
        filename=_Path(filepath).name,
        media_type="application/octet-stream",
    )
