from __future__ import annotations
import importlib, importlib.util, os
from pathlib import Path
from typing import Any
from backend.services.creator_tool_registry import get_tool_capability
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import ToolPoolAddToolRequest, ToolPoolGateEvent

RESOURCE_ROLES = {'reference','asset','skill_overview'}
SUGGESTED = {'read_pdf_text':'extract_pdf_text','read_xlsx_text':'read_spreadsheet','read_txt_text':'read_file_text','read_excel_text':'read_spreadsheet'}

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
    for fn in cap.functions or []:
        import_path=str(getattr(fn,'import_path','') or '').strip()
        function_name=str(getattr(fn,'function_name','') or '').strip()
        if not import_path or import_path == 'backend.services.runtime_tools':
            continue
        checked_paths.append(import_path); checked_functions.append(function_name)
        adapter_path=str(getattr(cap,'adapter_path','') or '')
        if '.bak' in adapter_path or 'custom_tools.bak' in adapter_path:
            messages.append('custom tool adapter is only present in a .bak path')
            continue
        try:
            module=importlib.import_module(import_path)
        except Exception as exc:
            messages.append(f'import_path not importable: {import_path}: {type(exc).__name__}: {exc}')
            continue
        if not function_name or not hasattr(module, function_name):
            messages.append(f'function not found in import_path: {import_path}.{function_name}')
            continue
        allowed_paths.append(import_path); allowed_functions.append(function_name)
    return allowed_paths, allowed_functions, checked_paths, checked_functions, messages

def gate_tool_request(request: ToolPoolAddToolRequest | dict[str, Any], *, file_role: str = 'generic_script', file_spec: dict[str, Any] | None = None) -> ToolPoolGateEvent:
    req = request if isinstance(request, ToolPoolAddToolRequest) else ToolPoolAddToolRequest(**request)
    if not req.target_file.startswith('scripts/') or file_role in RESOURCE_ROLES:
        return ToolPoolGateEvent(decision='blocked_by_policy', tool_id=req.candidate_tool_id, target_file=req.target_file, messages=['runtime tools may only bind to scripts/**, never references/assets'], score=req.score, matched_features=req.matched_features)
    cap = get_tool_capability(req.candidate_tool_id)
    if cap is None:
        return ToolPoolGateEvent(decision='not_found', tool_id=req.candidate_tool_id, target_file=req.target_file, messages=['tool_id is not registered'], suggested_replacements=list(SUGGESTED.values()), score=req.score, matched_features=req.matched_features)
    if not cap.enabled_by_default or not cap.allow_creator_use or str(cap.approval_status or '').lower() in {'disabled','denied','blocked'}:
        return ToolPoolGateEvent(decision='blocked_by_policy', tool_id=cap.name, target_file=req.target_file, messages=['tool is disabled or not allowed for Creator use'], score=req.score, matched_features=req.matched_features)
    roles = set(cap.roles or []) | set(cap.allowed_roles or [])
    if roles and file_role and file_role not in roles:
        return ToolPoolGateEvent(decision='role_mismatch', tool_id=cap.name, target_file=req.target_file, messages=[f'tool role mismatch: {file_role} not in {sorted(roles)}'], score=req.score, matched_features=req.matched_features)
    helper_imports = list(cap.helper_imports or [])
    denied = [h for h in helper_imports if h not in set(RUNTIME_TOOLS_ALL)]
    if denied:
        return ToolPoolGateEvent(decision='deny', tool_id=cap.name, target_file=req.target_file, denied_helper_imports=denied, messages=['helper_imports are not exported by backend.services.runtime_tools.__all__'], suggested_replacements=[SUGGESTED[h] for h in denied if h in SUGGESTED], score=req.score, matched_features=req.matched_features)
    import_paths, function_imports, checked_paths, checked_functions, import_messages = _check_function_imports(cap)
    has_custom_functions=bool(checked_paths)
    if has_custom_functions and not import_paths:
        return ToolPoolGateEvent(decision='deny', tool_id=cap.name, target_file=req.target_file, checked_import_paths=checked_paths, checked_functions=checked_functions, messages=import_messages or ['custom tool import_path/function unavailable'], score=req.score, matched_features=req.matched_features)
    missing_env = [name for name in list(cap.required_env or []) + list(cap.required_secrets or []) if not os.environ.get(str(name))]
    missing_deps = _missing_deps(list(cap.dependencies or []))
    decision = 'allow'
    if missing_env: decision='require_config'
    elif missing_deps: decision='require_dependency'
    return ToolPoolGateEvent(decision=decision, tool_id=cap.name, target_file=req.target_file, allowed_helper_imports=helper_imports, allowed_import_paths=import_paths, allowed_function_imports=function_imports, checked_import_paths=checked_paths, checked_functions=checked_functions, required_env=list(cap.required_env or [])+list(cap.required_secrets or []), missing_env=missing_env, dependencies=list(cap.dependencies or []), missing_dependencies=missing_deps, messages=import_messages or ['allowed' if decision=='allow' else decision], score=req.score, matched_features=req.matched_features)
