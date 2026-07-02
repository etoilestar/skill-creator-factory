"""Creator context uploads.

These helpers persist files that are available to the Creator planning phase.
They intentionally do not create Skill assets, initialize Skills, mutate
blueprints, or change file plans.
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UPLOAD_ROOT = PROJECT_ROOT / "backend" / "data" / "creator_uploads"
MAX_CONTEXT_UPLOAD_BYTES = 25 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md", ".json", ".yaml", ".yml",
    ".png", ".jpg", ".jpeg", ".webp",
}

CANDIDATE_TOOLS_BY_EXTENSION = {
    ".pdf": ["pdf_parsing"],
    ".docx": ["docx_parsing"],
    ".pptx": ["pptx_parsing"],
    ".xlsx": ["spreadsheet_read"],
    ".csv": ["csv_read"],
    ".png": ["vision_understanding"],
    ".jpg": ["vision_understanding"],
    ".jpeg": ["vision_understanding"],
    ".webp": ["vision_understanding"],
}

CONTENT_KIND_BY_EXTENSION = {
    ".pdf": "document", ".docx": "document", ".pptx": "document",
    ".xlsx": "spreadsheet", ".csv": "data",
    ".txt": "text", ".md": "text", ".json": "data", ".yaml": "data", ".yml": "data",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
}


def sanitize_session_id(session_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "").strip()).strip(".-_")
    return value[:80] or uuid.uuid4().hex


def sanitize_filename(filename: str) -> str:
    base = Path(str(filename or "upload")).name
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(base).stem).strip("._") or "upload"
    suffix = Path(base).suffix.lower()
    return f"{stem[:80]}{suffix}"


def _suggested_role(extension: str) -> str:
    if extension in {".png", ".jpg", ".jpeg", ".webp", ".csv", ".xlsx"}:
        return "runtime_input"
    return "reference_only"


def save_creator_context_upload(*, fileobj: BinaryIO, filename: str, session_id: str, mime_type: str | None = None) -> dict[str, Any]:
    original_name = filename or "upload"
    clean_session = sanitize_session_id(session_id)
    clean_name = sanitize_filename(original_name)
    extension = Path(clean_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported Creator context upload extension: {extension or '(none)'}")

    file_id = uuid.uuid4().hex
    stored_name = f"{file_id}_{clean_name}"
    session_dir = (UPLOAD_ROOT / clean_session).resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    target = (session_dir / stored_name).resolve()
    if not target.is_relative_to(session_dir):
        raise ValueError("Invalid upload filename")

    total = 0
    with target.open("wb") as out:
        while True:
            chunk = fileobj.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONTEXT_UPLOAD_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise ValueError("Creator context upload is too large")
            out.write(chunk)

    detected_mime = mime_type or mimetypes.guess_type(clean_name)[0] or "application/octet-stream"
    return {
        "success": True,
        "file_id": file_id,
        "session_id": clean_session,
        "name": stored_name,
        "original_name": original_name,
        "path": str(target),
        "size": total,
        "mime_type": detected_mime,
        "extension": extension,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "suggested_role": _suggested_role(extension),
        "asset_decision": "unknown",
        "candidate_tools": CANDIDATE_TOOLS_BY_EXTENSION.get(extension, []),
        "content_kind": CONTENT_KIND_BY_EXTENSION.get(extension, "unknown"),
        "message": "Uploaded as Creator context only; not added to Skill assets.",
    }
