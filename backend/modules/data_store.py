"""JSON-file based session persistence with multi-session isolation."""
import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

from .config import SKILL_DATA_PATH

_lock = threading.Lock()

# Only allow UUIDs / alphanumeric+hyphen identifiers (max 64 chars)
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


def _sessions_dir() -> Path:
    d = Path(SKILL_DATA_PATH) / ".sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_session_path(session_id: str) -> Path:
    """Build and confine the session file path.

    Validates session_id format, then resolves the path and asserts it
    stays within the sessions directory to prevent path traversal.
    """
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id format: {session_id!r}")
    base = _sessions_dir().resolve()
    # Use only the basename portion of the id to strip any embedded separators
    safe_name = os.path.basename(session_id + ".json")
    candidate = (base / safe_name).resolve()
    # Path confinement: reject anything that escapes the sessions directory
    if not str(candidate).startswith(str(base) + os.sep) and candidate != base:
        raise ValueError(f"Path traversal detected for session_id: {session_id!r}")
    return candidate


def save_session(session_id: str, data: dict) -> None:
    """Persist session data as JSON."""
    path = _safe_session_path(session_id)
    with _lock:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str) -> Optional[dict]:
    """Load session data. Returns None if not found."""
    path = _safe_session_path(session_id)
    if not path.exists():
        return None
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))


def list_all_skills() -> list[str]:
    """Return names of all generated skill folders in skill-data/."""
    root = Path(SKILL_DATA_PATH)
    if not root.exists():
        return []
    return [
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]


def delete_session(session_id: str) -> None:
    """Remove a session file."""
    path = _safe_session_path(session_id)
    with _lock:
        path.unlink(missing_ok=True)
