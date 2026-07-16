from __future__ import annotations
import importlib, importlib.util, os
from pathlib import Path
from typing import Any


def _uniq(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        if value not in out:
            out.append(value)
    return out
from backend.services.creator_tool_registry import get_tool_capability
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import ToolPoolAddToolRequest, ToolPoolGateEvent


def _missing_deps(deps: list[Any]) -> list[str]:
    missing=[]
    for dep in deps or []:
        imports = dep.get('imports') if isinstance(dep, dict) else [str(dep)] if isinstance(dep, str) else []
        for name in imports or []:
            try:
                if importlib.util.find_spec(str(name)) is None:
                    missing.append(str(name))
            except Exception:
                missing.append(str(name))
    return missing

def _check_function_imports(cap: Any) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    allowed_paths=[]; allowed_functions=[]; checked_paths=[]; checked_functions=[]; messages=[]
    runtime_tools_all = set(RUNTIME_TOOLS_ALL)
    for fn in cap.functions or []:
        import_path=str(getattr(fn,'import_path','') or '').strip()
        function_name=str(getattr(fn,'function_name','') or '').strip()
        if not import_path or not function_name:
            continue
        checked_paths.append(import_path); checked_functions.append(function_name)
        if import_path == 'backend.services.runtime_tools':
            if function_name not in runtime_tools_all:
                messages.append(f'function not exported by backend.services.runtime_tools: {function_name}')
                continue
            allowed_paths.append(import_path); allowed_functions.append(function_name)
            continue
        adapter_path=str(getattr(cap,'adapter_path','') or '')
        if '.bak' in adapter_path or 'custom_tools.bak' in adapter_path:
            messages.append('custom tool adapter is only present in a .bak path')
            continue
        try:
            module=importlib.import_module(import_path)
        except Exception as exc:
            messages.append(f'import_path not importable: {import_path}: {type(exc).__name__}: {exc}')
            continue
        if not hasattr(module, function_name):
            messages.append(f'function not found in import_path: {import_path}.{function_name}')
            continue
        allowed_paths.append(import_path); allowed_functions.append(function_name)
    return _uniq(allowed_paths), _uniq(allowed_functions), _uniq(checked_paths), _uniq(checked_functions), messages

def gate_tool_request(
    request: ToolPoolAddToolRequest | dict[str, Any],
    *,
    file_role: str = "generic_script",
    file_spec: dict[str, Any] | None = None,
) -> ToolPoolGateEvent:
    """Gate one candidate tool for the current Skill ToolPool.

    Tool authorization scope is Skill-wide.

    file_role and file_spec are retained only for call-site compatibility and
    are not authorization inputs.

    Planner chooses an exact Registry tool ID.
    Backend Gate only verifies:
    - Registry identity;
    - Creator availability/policy;
    - exported runtime helpers;
    - callable import availability;
    - required environment/secrets;
    - dependencies.
    """

    _ = file_role
    _ = file_spec

    req = (
        request
        if isinstance(
            request,
            ToolPoolAddToolRequest,
        )
        else ToolPoolAddToolRequest(
            **request
        )
    )

    candidate_tool_id = str(
        req.candidate_tool_id or ""
    ).strip()

    capability = get_tool_capability(
        candidate_tool_id
    )

    if capability is None:
        return ToolPoolGateEvent(
            decision="not_found",
            tool_id=candidate_tool_id,
            target_file="",
            messages=[
                "tool_id is not registered"
            ],
            suggested_replacements=[],
            score=req.score,
            matched_features=list(
                req.matched_features or []
            ),
        )

    if (
        not capability.enabled_by_default
        or not capability.allow_creator_use
        or str(
            capability.approval_status
            or ""
        ).lower()
        in {
            "disabled",
            "denied",
            "blocked",
        }
    ):
        return ToolPoolGateEvent(
            decision="blocked_by_policy",
            tool_id=capability.name,
            target_file="",
            messages=[
                "tool is disabled or not allowed "
                "for Creator use"
            ],
            score=req.score,
            matched_features=list(
                req.matched_features or []
            ),
        )

    (
        allowed_import_paths,
        allowed_function_imports,
        checked_import_paths,
        checked_functions,
        import_messages,
    ) = _check_function_imports(
        capability
    )

    has_manifest_functions = bool(
        checked_import_paths
    )

    if (
        has_manifest_functions
        and not allowed_function_imports
    ):
        return ToolPoolGateEvent(
            decision="deny",
            tool_id=capability.name,
            target_file="",
            checked_import_paths=(
                checked_import_paths
            ),
            checked_functions=(
                checked_functions
            ),
            messages=(
                import_messages
                or [
                    (
                        "custom tool import_path/"
                        "function unavailable"
                    )
                ]
            ),
            score=req.score,
            matched_features=list(
                req.matched_features or []
            ),
        )


    manifest_runtime_helpers = []
    for function in capability.functions or []:
        function_name = str(getattr(function, 'function_name', '') or '').strip()
        import_path = str(getattr(function, 'import_path', '') or '').strip()
        if (
            import_path == 'backend.services.runtime_tools'
            and import_path in allowed_import_paths
            and function_name in allowed_function_imports
        ):
            manifest_runtime_helpers.append(function_name)
    manifest_runtime_helpers = _uniq(manifest_runtime_helpers)

    missing_env = [
        str(name)
        for name in [
            *list(
                capability.required_env
                or []
            ),
            *list(
                capability.required_secrets
                or []
            ),
        ]
        if not os.environ.get(
            str(name)
        )
    ]

    missing_dependencies = _missing_deps(
        list(
            capability.dependencies
            or []
        )
    )

    decision = "allow"

    if missing_env:
        decision = "require_config"

    elif missing_dependencies:
        decision = "require_dependency"

    final_messages = list(
        import_messages or []
    )

    if not final_messages:
        final_messages = [
            (
                "allowed"
                if decision == "allow"
                else decision
            )
        ]

    return ToolPoolGateEvent(
        decision=decision,
        tool_id=capability.name,
        target_file="",
        allowed_helper_imports=(
            manifest_runtime_helpers
        ),
        allowed_import_paths=(
            allowed_import_paths
        ),
        allowed_function_imports=(
            allowed_function_imports
        ),
        checked_import_paths=(
            checked_import_paths
        ),
        checked_functions=(
            checked_functions
        ),
        required_env=[
            *list(
                capability.required_env
                or []
            ),
            *list(
                capability.required_secrets
                or []
            ),
        ],
        missing_env=missing_env,
        dependencies=list(
            capability.dependencies
            or []
        ),
        missing_dependencies=(
            missing_dependencies
        ),
        messages=final_messages,
        score=req.score,
        matched_features=list(
            req.matched_features or []
        ),
    )
