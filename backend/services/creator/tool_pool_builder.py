from __future__ import annotations
from typing import Any
from backend.services.creator_tool_registry import get_tool_capability
from .tool_pool_models import ToolPoolModel, ToolPoolTool, ToolPoolFileBinding, ToolPoolDeniedRequest, ToolPoolMissingRequest
from .tool_pool_explorer import explore_tool_pool
from .tool_pool_gate import gate_tool_request

CORE_HELPERS = ['strict_json_argv_guard']

def _uniq(values: list[Any]) -> list[Any]:
    out=[]
    for v in values:
        if v not in out: out.append(v)
    return out

def build_tool_pool(
    *,
    skill_name: str = "",
    user_request: str = "",
    blueprint_text: str = "",
    file_specs: list[dict[str, Any]] | None = None,
    uploaded_files: list[dict[str, Any]] | None = None,
    current_tool_pool: ToolPoolModel | None = None,
) -> ToolPoolModel:
    """Synchronize file projections for the current shared Skill ToolPool.

    Important:
    - This function does not discover tools.
    - This function does not semantically select tools.
    - This function does not create new tool authorization.

    Tool discovery belongs to the planning model.
    Tool authorization belongs to backend gate.
    ToolPool.tools is preserved from the current persisted shared pool.

    The builder only synchronizes script projection records.
    """
    pool = (
        current_tool_pool.model_copy(deep=True)
        if current_tool_pool is not None
        else ToolPoolModel(skill_name=skill_name)
    )

    if skill_name:
        pool.skill_name = skill_name

    specs_by_path: dict[str, dict[str, Any]] = {}

    for raw_spec in file_specs or []:
        if not isinstance(raw_spec, dict):
            continue

        target_file = str(
            raw_spec.get("path")
            or raw_spec.get("target_file")
            or ""
        ).strip()

        if not target_file.startswith("scripts/"):
            continue

        specs_by_path[target_file] = raw_spec

    existing_bindings = {
        binding.target_file: binding
        for binding in pool.file_bindings
    }

    synchronized_bindings: list[ToolPoolFileBinding] = []

    for target_file, spec in specs_by_path.items():
        binding = existing_bindings.get(target_file)

        if binding is None:
            binding = ToolPoolFileBinding(
                target_file=target_file,
            )
        else:
            binding = binding.model_copy(deep=True)

        binding.allowed_tool_ids = _uniq([
            "script_argv_guard",
            *(binding.allowed_tool_ids or []),
        ])

        binding.primary_tool_ids = _uniq([
            "script_argv_guard",
            *(binding.primary_tool_ids or []),
        ])

        binding.allowed_helper_imports = _uniq([
            *CORE_HELPERS,
            *(binding.allowed_helper_imports or []),
        ])

        if isinstance(spec.get("inputs"), dict):
            binding.input_schema = dict(spec["inputs"])

        if isinstance(spec.get("outputs"), dict):
            binding.output_schema = dict(spec["outputs"])

        synchronized_bindings.append(binding)

    pool.file_bindings = synchronized_bindings

    valid_script_paths = set(specs_by_path)

    for tool in pool.tools:
        tool.target_files = [
            target_file
            for target_file in (tool.target_files or [])
            if target_file in valid_script_paths
        ]

    return pool
