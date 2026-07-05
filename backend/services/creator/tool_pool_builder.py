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
    """Preserve the current shared ToolPool and ensure script bindings exist.

    Tool discovery is owned by the planning model.

    This function must not:
    - explore registry tools;
    - select business tools;
    - Gate new business tools.

    It only preserves the current Skill ToolPool and creates missing
    per-script ranking records.
    """

    _ = user_request
    _ = blueprint_text
    _ = uploaded_files

    pool = (
        current_tool_pool.model_copy(deep=True)
        if current_tool_pool is not None
        else ToolPoolModel(skill_name=skill_name)
    )

    pool.skill_name = skill_name

    existing_targets = {
        binding.target_file
        for binding in pool.file_bindings
    }

    valid_script_targets: set[str] = set()

    for spec in file_specs or []:
        target = str(
            spec.get("path")
            or spec.get("target_file")
            or ""
        ).strip()

        if not target.startswith("scripts/"):
            continue

        valid_script_targets.add(target)

        if target in existing_targets:
            continue

        pool.file_bindings.append(
            ToolPoolFileBinding(
                target_file=target,
                allowed_tool_ids=[
                    "script_argv_guard",
                ],
                primary_tool_ids=[
                    "script_argv_guard",
                ],
                allowed_helper_imports=list(
                    CORE_HELPERS
                ),
                input_schema=(
                    spec.get("inputs")
                    if isinstance(
                        spec.get("inputs"),
                        dict,
                    )
                    else {}
                ),
                output_schema=(
                    spec.get("outputs")
                    if isinstance(
                        spec.get("outputs"),
                        dict,
                    )
                    else {}
                ),
            )
        )

    pool.file_bindings = [
        binding
        for binding in pool.file_bindings
        if binding.target_file
        in valid_script_targets
    ]

    return pool
