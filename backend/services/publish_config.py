"""Publish configuration management service.

Manages persistent publish endpoint configurations: which skills are enabled,
model names, API keys, and active/inactive state.
Storage: JSON files under .skill-governance/publish/
"""

import json
import secrets
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..config import settings
from .skill_governance import list_skills_for_mode, EXECUTABLE_STATUSES


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _publish_dir() -> Path:
    root = settings.publish_config_path
    root.mkdir(parents=True, exist_ok=True)
    return root


def _configs_file() -> Path:
    return _publish_dir() / "configs.json"


def _generate_api_key() -> str:
    """Generate a secure random API key prefixed with 'sk-pub-'."""
    return f"sk-pub-{secrets.token_urlsafe(32)}"


def _load_all() -> dict:
    """Load all publish configs from disk."""
    path = _configs_file()
    if not path.exists():
        return {"configs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_all(data: dict) -> None:
    """Atomically save all publish configs to disk."""
    path = _configs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        Path(tmp.name).replace(path)


def load_publish_configs() -> list[dict]:
    """Return all publish configurations as a list."""
    data = _load_all()
    return list(data.get("configs", {}).values())


def get_publish_config(endpoint_id: str) -> dict | None:
    """Get a single publish config by endpoint_id."""
    data = _load_all()
    return data.get("configs", {}).get(endpoint_id)


def get_active_config(endpoint_id: str) -> dict | None:
    """Get a config only if it is active."""
    config = get_publish_config(endpoint_id)
    if config and config.get("is_active"):
        return config
    return None


def get_config_by_model_name(model_name: str) -> dict | None:
    """Find an active config by its published model name."""
    for config in load_publish_configs():
        if config.get("name") == model_name and config.get("is_active"):
            return config
    return None


def get_config_by_api_key(api_key: str) -> dict | None:
    """Find a config by its API key."""
    for config in load_publish_configs():
        if config.get("api_key") == api_key:
            return config
    return None


def save_publish_config(config: dict) -> dict:
    """Create or update a publish config. Returns the saved config."""
    data = _load_all()
    now = _now()

    if "endpoint_id" not in config or not config["endpoint_id"]:
        config["endpoint_id"] = str(uuid.uuid4())
        config["created_at"] = now
        config.setdefault("api_key", _generate_api_key())

    config["updated_at"] = now
    config.setdefault("name", f"model-{config['endpoint_id'][:8]}")
    config.setdefault("enabled_skills", [])
    config.setdefault("is_active", False)

    data.setdefault("configs", {})[config["endpoint_id"]] = config
    _save_all(data)
    return deepcopy(config)


def delete_publish_config(endpoint_id: str) -> bool:
    """Delete a publish config. Returns True if found and deleted."""
    data = _load_all()
    if endpoint_id in data.get("configs", {}):
        del data["configs"][endpoint_id]
        _save_all(data)
        return True
    return False


def toggle_publish_config(endpoint_id: str) -> dict | None:
    """Toggle active state of a config. Returns updated config or None."""
    config = get_publish_config(endpoint_id)
    if not config:
        return None
    config["is_active"] = not config.get("is_active", False)
    return save_publish_config(config)


def regenerate_api_key(endpoint_id: str) -> dict | None:
    """Regenerate the API key for a config. Returns updated config or None."""
    config = get_publish_config(endpoint_id)
    if not config:
        return None
    config["api_key"] = _generate_api_key()
    return save_publish_config(config)


def validate_skills_available(skill_names: list[str]) -> list[str]:
    """Validate which skills are currently approved and available.

    Returns the list of skill names that are valid (approved status).
    """
    approved_skills = list_skills_for_mode("manage")
    approved_names = {
        s["name"] for s in approved_skills
        if s.get("status") in EXECUTABLE_STATUSES
    }
    return [name for name in skill_names if name in approved_names]


def get_available_skills() -> list[dict]:
    """Get all approved skills available for publishing."""
    skills = list_skills_for_mode("manage")
    return [
        {
            "name": s["name"],
            "display_name": s.get("display_name", s["name"]),
            "description": s.get("description", ""),
            "version": s.get("version", "0.1.0"),
            "scope": s.get("scope", "managed"),
        }
        for s in skills
        if s.get("status") in EXECUTABLE_STATUSES
    ]
