from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..services.skill_manager import (
    delete_asset,
    delete_skill,
    get_asset,
    get_skill,
    list_skill_assets,
    list_skills,
    save_asset,
    save_skill,
    update_asset,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


class SaveSkillRequest(BaseModel):
    name: str
    content: str


@router.get("")
async def get_all_skills():
    """List all skills in the skills directory."""
    return list_skills()


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
