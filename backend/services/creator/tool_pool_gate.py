from __future__ import annotations
import importlib.util, os
from typing import Any
from backend.services.creator_tool_registry import get_tool_capability
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import ToolPoolAddToolRequest, ToolPoolGateEvent

RESOURCE_ROLES = {'reference','asset','skill_overview'}
SUGGESTED = {'read_pdf_text':'extract_pdf_text','read_xlsx_text':'read_spreadsheet','read_txt_text':'read_file_text','read_excel_text':'read_spreadsheet'}

def _missing_deps(deps: list[Any]) -> list[str]:
    missing=[]
    for dep in deps or []:
        imports = dep.get('imports') if isinstance(dep, dict) else []
        for name in imports or []:
            if importlib.util.find_spec(str(name)) is None:
                missing.append(str(name))
    return missing

def gate_tool_request(request: ToolPoolAddToolRequest | dict[str, Any], *, file_role: str = 'generic_script', file_spec: dict[str, Any] | None = None) -> ToolPoolGateEvent:
    req = request if isinstance(request, ToolPoolAddToolRequest) else ToolPoolAddToolRequest(**request)
    messages=[]
    if not req.target_file.startswith('scripts/') or file_role in RESOURCE_ROLES:
        return ToolPoolGateEvent(decision='blocked_by_policy', tool_id=req.candidate_tool_id, target_file=req.target_file, messages=['runtime tools may only bind to scripts/**, never references/assets'])
    cap = get_tool_capability(req.candidate_tool_id)
    if cap is None:
        return ToolPoolGateEvent(decision='not_found', tool_id=req.candidate_tool_id, target_file=req.target_file, messages=['tool_id is not registered'], suggested_replacements=list(SUGGESTED.values()))
    if not cap.enabled_by_default or not cap.allow_creator_use:
        return ToolPoolGateEvent(decision='blocked_by_policy', tool_id=cap.name, target_file=req.target_file, messages=['tool is disabled or not allowed for Creator use'])
    roles = set(cap.roles or []) | set(cap.allowed_roles or [])
    if roles and file_role and file_role not in roles and 'generic_script' not in roles:
        return ToolPoolGateEvent(decision='role_mismatch', tool_id=cap.name, target_file=req.target_file, messages=[f'tool role mismatch: {file_role} not in {sorted(roles)}'])
    helper_imports = list(cap.helper_imports or [])
    denied = [h for h in helper_imports if h not in set(RUNTIME_TOOLS_ALL)]
    if denied:
        return ToolPoolGateEvent(decision='deny', tool_id=cap.name, target_file=req.target_file, denied_helper_imports=denied, messages=['helper_imports are not exported by backend.services.runtime_tools.__all__'], suggested_replacements=[SUGGESTED[h] for h in denied if h in SUGGESTED])
    missing_env = [name for name in (cap.required_env or cap.required_secrets or []) if not os.environ.get(str(name))]
    missing_deps = _missing_deps(list(cap.dependencies or []))
    decision = 'allow'
    if missing_env: decision='require_config'
    elif missing_deps: decision='require_dependency'
    return ToolPoolGateEvent(decision=decision, tool_id=cap.name, target_file=req.target_file, allowed_helper_imports=helper_imports, required_env=list(cap.required_env or [])+list(cap.required_secrets or []), missing_env=missing_env, dependencies=list(cap.dependencies or []), missing_dependencies=missing_deps, messages=messages or ['allowed' if decision=='allow' else decision])
