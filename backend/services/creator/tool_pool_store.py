from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from .tool_pool_models import ToolPoolModel, ToolPoolFileBinding, ToolPoolGateEvent, ToolPoolDeniedRequest, ToolPoolMissingRequest, utc_now_iso

TOOL_POOL_RELATIVE_PATH = Path('.creator') / 'tool_pool.json'

def tool_pool_path(skill_dir: str | Path) -> Path:
    return Path(skill_dir) / TOOL_POOL_RELATIVE_PATH

def save_tool_pool(skill_dir: str | Path, pool: ToolPoolModel) -> Path:
    path = tool_pool_path(skill_dir); path.parent.mkdir(parents=True, exist_ok=True)
    pool.updated_at = utc_now_iso()
    data = pool.model_dump(mode='json')
    fd, tmp = tempfile.mkstemp(prefix='tool_pool.', suffix='.json', dir=str(path.parent))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)
    return path

def load_tool_pool(skill_dir: str | Path) -> ToolPoolModel:
    path = tool_pool_path(skill_dir)
    if not path.is_file():
        return ToolPoolModel()
    return ToolPoolModel.model_validate_json(path.read_text(encoding='utf-8'))

def get_file_binding(pool: ToolPoolModel, target_file: str) -> ToolPoolFileBinding | None:
    return next((b for b in pool.file_bindings if b.target_file == target_file), None)

def get_allowed_helper_imports(pool: ToolPoolModel, target_file: str) -> list[str]:
    binding = get_file_binding(pool, target_file)
    return list(binding.allowed_helper_imports) if binding else []

def add_gate_event(pool: ToolPoolModel, event: ToolPoolGateEvent) -> None:
    pool.gate_events.append(event); pool.updated_at = utc_now_iso()

def add_denied_request(pool: ToolPoolModel, denied: ToolPoolDeniedRequest) -> None:
    pool.denied_requests.append(denied); pool.updated_at = utc_now_iso()

def add_missing_request(pool: ToolPoolModel, missing: ToolPoolMissingRequest) -> None:
    pool.missing_requests.append(missing); pool.updated_at = utc_now_iso()
