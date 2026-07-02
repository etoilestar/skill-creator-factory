from __future__ import annotations
from typing import Any
from backend.services.creator_tool_registry import get_tool_capability
from .tool_pool_models import ToolPoolModel, ToolPoolTool, ToolPoolFileBinding, ToolPoolDeniedRequest, ToolPoolMissingRequest
from .tool_pool_explorer import explore_tool_pool
from .tool_pool_gate import gate_tool_request

CORE_HELPERS = ['strict_json_argv_guard']

def build_tool_pool(*, skill_name: str = '', user_request: str = '', blueprint_text: str = '', file_specs: list[dict[str, Any]] | None = None, uploaded_files: list[dict[str, Any]] | None = None) -> ToolPoolModel:
    pool=ToolPoolModel(skill_name=skill_name)
    bindings: dict[str, ToolPoolFileBinding] = {}
    for spec in file_specs or []:
        target=str(spec.get('path') or spec.get('target_file') or '')
        if target.startswith('scripts/'):
            bindings[target]=ToolPoolFileBinding(target_file=target, allowed_tool_ids=['script_argv_guard'], allowed_helper_imports=list(CORE_HELPERS), input_schema=spec.get('inputs') if isinstance(spec.get('inputs'), dict) else {}, output_schema=spec.get('outputs') if isinstance(spec.get('outputs'), dict) else {})
    exploration=explore_tool_pool(user_request=user_request, blueprint_text=blueprint_text, file_specs=file_specs, uploaded_files=uploaded_files)
    for req in exploration.candidate_tool_requests:
        spec = next((s for s in (file_specs or []) if str(s.get('path') or s.get('target_file') or '') == req.target_file), {})
        event=gate_tool_request(req, file_role=str(spec.get('role') or 'generic_script'), file_spec=spec)
        pool.gate_events.append(event)
        if event.decision == 'allow':
            cap=get_tool_capability(event.tool_id)
            pool.tools.append(ToolPoolTool(tool_id=event.tool_id, status='allowed', source=req.source, source_phase='blueprint', target_files=[req.target_file], allowed_helper_imports=event.allowed_helper_imports, allowed_roles=list((cap.roles if cap else []) or []), input_schema=(cap.input_schema if cap else {}) or {}, output_schema=(cap.output_schema if cap else {}) or {}, required_env=event.required_env, dependencies=event.dependencies, reason=req.reason, gate_result=event.decision, gate_messages=event.messages))
            b=bindings.setdefault(req.target_file, ToolPoolFileBinding(target_file=req.target_file, allowed_helper_imports=list(CORE_HELPERS)))
            b.allowed_tool_ids = sorted(set(b.allowed_tool_ids + [event.tool_id]))
            b.allowed_helper_imports = sorted(set(b.allowed_helper_imports + event.allowed_helper_imports))
            b.required_env = sorted(set(b.required_env + event.required_env))
            b.dependencies = b.dependencies + [d for d in event.dependencies if d not in b.dependencies]
            if cap:
                b.snippets.extend([s.__dict__ for s in (cap.snippets or [])])
        elif event.decision in {'require_config','require_dependency'}:
            pool.missing_requests.append(ToolPoolMissingRequest(target_file=req.target_file, tool_id=event.tool_id, missing_env=event.missing_env, missing_dependencies=event.missing_dependencies, reason='; '.join(event.messages)))
        else:
            pool.denied_requests.append(ToolPoolDeniedRequest(target_file=req.target_file, tool_id=event.tool_id, helper_imports=event.denied_helper_imports, reason=event.decision, messages=event.messages, suggested_replacements=event.suggested_replacements))
    pool.file_bindings=list(bindings.values())
    return pool
