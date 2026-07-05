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

def add_gate_event(pool: ToolPoolModel, event: ToolPoolGateEvent) -> None:
    pool.gate_events.append(event); pool.updated_at = utc_now_iso()

def add_denied_request(pool: ToolPoolModel, denied: ToolPoolDeniedRequest) -> None:
    pool.denied_requests.append(denied); pool.updated_at = utc_now_iso()

def add_missing_request(pool: ToolPoolModel, missing: ToolPoolMissingRequest) -> None:
    pool.missing_requests.append(missing); pool.updated_at = utc_now_iso()


def _stable_unique(values):
    out = []
    for value in values or []:
        if value not in out:
            out.append(value)
    return out


def get_file_binding(
    pool: ToolPoolModel,
    target_file: str,
    *,
    raw: bool = False,
) -> ToolPoolFileBinding | None:
    """Return the target-file projection of the shared Skill ToolPool.

    ToolPool.tools is the single authorization source for the current Skill.

    file_bindings only keeps target-file preference/ranking metadata:
    - primary_tool_ids
    - secondary_tool_ids
    - scored_tools
    - matched_features_by_tool

    It must not narrow the Skill-wide callable tool set.

    raw=True is reserved for backend mutation code that needs the persisted
    projection object itself.
    """
    stored = next(
        (
            binding
            for binding in pool.file_bindings
            if binding.target_file == target_file
        ),
        None,
    )

    if raw:
        return stored

    is_script = str(target_file or "").startswith("scripts/")

    if stored is None and not is_script:
        return None

    binding = (
        stored.model_copy(deep=True)
        if stored is not None
        else ToolPoolFileBinding(target_file=target_file)
    )

    allowed_tools = [
        tool
        for tool in pool.tools
        if tool.status == "allowed"
    ]

    core_tool_ids = ["script_argv_guard"] if is_script else []
    core_helper_imports = ["strict_json_argv_guard"] if is_script else []

    shared_tool_ids = _stable_unique([
        tool.tool_id
        for tool in allowed_tools
        if str(tool.tool_id or "").strip()
    ])

    binding.allowed_tool_ids = _stable_unique([
        *core_tool_ids,
        *shared_tool_ids,
    ])

    allowed_id_set = set(binding.allowed_tool_ids)

    preferred_primary = [
        tool_id
        for tool_id in (binding.primary_tool_ids or [])
        if tool_id in allowed_id_set
    ]

    binding.primary_tool_ids = _stable_unique([
        *core_tool_ids,
        *preferred_primary,
    ])

    primary_set = set(binding.primary_tool_ids)

    preferred_secondary = [
        tool_id
        for tool_id in (binding.secondary_tool_ids or [])
        if tool_id in allowed_id_set
        and tool_id not in primary_set
    ]

    shared_secondary = [
        tool_id
        for tool_id in shared_tool_ids
        if tool_id not in primary_set
    ]

    binding.secondary_tool_ids = _stable_unique([
        *preferred_secondary,
        *shared_secondary,
    ])

    binding.allowed_helper_imports = _stable_unique([
        *core_helper_imports,
        *[
            helper
            for tool in allowed_tools
            for helper in (tool.allowed_helper_imports or [])
        ],
    ])

    binding.allowed_import_paths = _stable_unique([
        import_path
        for tool in allowed_tools
        for import_path in (tool.allowed_import_paths or [])
    ])

    binding.allowed_function_imports = _stable_unique([
        function_import
        for tool in allowed_tools
        for function_import in (tool.allowed_function_imports or [])
    ])

    binding.required_env = _stable_unique([
        env_name
        for tool in allowed_tools
        for env_name in (tool.required_env or [])
    ])

    binding.dependencies = _stable_unique([
        dependency
        for tool in allowed_tools
        for dependency in (tool.dependencies or [])
    ])

    return binding


def get_allowed_helper_imports(
    pool: ToolPoolModel,
    target_file: str,
) -> list[str]:
    binding = get_file_binding(pool, target_file)
    return list(binding.allowed_helper_imports) if binding else []


def tool_pool_snapshot(pool: ToolPoolModel) -> dict:
    """Return the canonical current-Skill ToolPool view for every model/backend.

    Planner, code model and responsibility judge should receive projections
    derived from this same persisted pool.
    """
    target_files = _stable_unique([
        *[
            binding.target_file
            for binding in pool.file_bindings
            if str(binding.target_file or "").strip()
        ],
        *[
            target_file
            for tool in pool.tools
            for target_file in (tool.target_files or [])
            if str(target_file or "").strip()
        ],
    ])

    effective_bindings = []

    for target_file in target_files:
        binding = get_file_binding(pool, target_file)
        if binding is not None:
            effective_bindings.append(
                binding.model_dump(mode="json")
            )

    return {
        "skill_name": pool.skill_name,
        "version": pool.version,
        "source": pool.source,
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
        "tools": [
            tool.model_dump(mode="json")
            for tool in pool.tools
            if tool.status == "allowed"
        ],
        "file_bindings": effective_bindings,
        "denied_requests": [
            item.model_dump(mode="json")
            for item in pool.denied_requests
        ],
        "missing_requests": [
            item.model_dump(mode="json")
            for item in pool.missing_requests
        ],
        "gate_events": [
            item.model_dump(mode="json")
            for item in pool.gate_events
        ],
        "exploration_candidates": list(
            pool.exploration_candidates or []
        ),
        "scored_candidates": list(
            pool.scored_candidates or []
        ),
        "uploaded_file_triggers": list(
            pool.uploaded_file_triggers or []
        ),
    }
