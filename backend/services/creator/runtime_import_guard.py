"""Static guard for generated imports from platform runtime helper namespaces."""
from __future__ import annotations
import ast
from typing import Any
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import RuntimeImportGuardResult, ToolPoolFileBinding

SUGGESTED_REPLACEMENTS = {
    'read_pdf_text': 'extract_pdf_text',
    'read_xlsx_text': 'read_spreadsheet',
    'read_excel_text': 'read_spreadsheet',
    'read_txt_text': 'read_file_text',
}
_ALLOWED_SKILL_RUNTIME_HELPERS = {'generate_text_with_llm'}

def _binding_allowed(binding: ToolPoolFileBinding | dict[str, Any] | None) -> list[str]:
    if binding is None:
        return list(RUNTIME_TOOLS_ALL)
    if isinstance(binding, ToolPoolFileBinding):
        return list(binding.allowed_helper_imports or [])
    return [str(x) for x in binding.get('allowed_helper_imports') or []]

def guard_runtime_imports(source: str, target_file: str, file_binding: ToolPoolFileBinding | dict[str, Any] | None = None) -> RuntimeImportGuardResult:
    allowed = set(_binding_allowed(file_binding))
    runtime_all = set(RUNTIME_TOOLS_ALL)
    missing: list[str] = []
    forbidden: list[str] = []
    warnings: list[str] = []
    try:
        tree = ast.parse(source or '')
    except SyntaxError as exc:
        return RuntimeImportGuardResult(success=False, error_type='generated_python_syntax_error', target_file=target_file, allowed_helper_imports=sorted(allowed), repair_instruction=str(exc))
    runtime_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if module == 'backend.services.runtime_tools':
                for alias in node.names:
                    name = alias.name
                    if name == '*':
                        forbidden.append('*')
                    elif name not in runtime_all:
                        missing.append(name)
                    elif name not in allowed:
                        forbidden.append(name)
            elif module == 'backend.services.skill_runtime':
                for alias in node.names:
                    if alias.name not in _ALLOWED_SKILL_RUNTIME_HELPERS:
                        forbidden.append(f'skill_runtime.{alias.name}')
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'backend.services.runtime_tools':
                    runtime_aliases.add(alias.asname or alias.name.split('.')[-1])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'backend.services.runtime_tools':
                    warnings.append('__import__ runtime_tools usage is not allowed for generated helpers')
                    forbidden.append('__import__(backend.services.runtime_tools)')
            if isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in runtime_aliases and isinstance(node.args[1], ast.Constant):
                    name = str(node.args[1].value)
                    if name not in runtime_all:
                        missing.append(name)
                    elif name not in allowed:
                        forbidden.append(name)
    missing = sorted(set(missing)); forbidden = sorted(set(forbidden))
    if missing or forbidden:
        err = 'generated_unknown_runtime_tool_import' if missing else 'generated_pool_forbidden_import'
        suggestions = sorted({SUGGESTED_REPLACEMENTS[x] for x in missing + forbidden if x in SUGGESTED_REPLACEMENTS})
        return RuntimeImportGuardResult(success=False, error_type=err, target_file=target_file, missing_imports=missing, forbidden_imports=forbidden, allowed_helper_imports=sorted(allowed), suggested_replacements=suggestions, repair_instruction='Use only Current File Tool Binding allowed_helper_imports; request a tool_pool_patch for missing capabilities.', warnings=warnings)
    return RuntimeImportGuardResult(success=True, target_file=target_file, allowed_helper_imports=sorted(allowed), warnings=warnings)
