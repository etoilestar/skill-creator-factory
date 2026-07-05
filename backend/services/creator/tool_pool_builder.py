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
    """Preserve and normalize the current Skill-wide ToolPool.

    Tool discovery is owned by the planning model.

    Backend Gate is the only authorization authority.

    This function does not:
    - explore Registry tools;
    - select business tools;
    - Gate new business tools;
    - create per-file authorization bindings.

    ToolPool.tools is the single Skill-wide authorization source.
    """

    _ = user_request
    _ = blueprint_text
    _ = file_specs
    _ = uploaded_files

    pool = (
        current_tool_pool.model_copy(
            deep=True
        )
        if current_tool_pool is not None
        else ToolPoolModel(
            skill_name=skill_name
        )
    )

    pool.skill_name = str(
        skill_name or ""
    )

    # Per-file authorization is retired.
    # Drop historical persisted bindings while keeping the model field for
    # backward-compatible JSON loading.
    pool.file_bindings = []

    # target_files was historical ranking/binding metadata.
    # Tool authorization now belongs to the Skill.
    for tool in pool.tools:
        tool.target_files = []

    for denied in pool.denied_requests:
        denied.target_file = ""

    for missing in pool.missing_requests:
        missing.target_file = ""

    for event in pool.gate_events:
        event.target_file = ""

    pool.exploration_candidates = [
        {
            key: value
            for key, value in row.items()
            if key != "target_file"
        }
        for row in (
            pool.exploration_candidates
            or []
        )
        if isinstance(row, dict)
    ]

    pool.scored_candidates = [
        {
            key: value
            for key, value in row.items()
            if key != "target_file"
        }
        for row in (
            pool.scored_candidates
            or []
        )
        if isinstance(row, dict)
    ]

    return pool
