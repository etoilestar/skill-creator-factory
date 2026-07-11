"""Static guard for generated imports from platform runtime helper namespaces."""
from __future__ import annotations
import ast
from typing import Any
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import RuntimeImportGuardResult, ToolPoolFileBinding

_CUSTOM_PREFIX = 'backend.services.runtime_tools.custom_tools'

def _binding_values(binding: ToolPoolFileBinding | dict[str, Any] | None, key: str, default: list[str] | None = None) -> list[str]:
    if binding is None: return list(default or [])
    if isinstance(binding, ToolPoolFileBinding): return list(getattr(binding, key, []) or [])
    return [str(x) for x in binding.get(key) or []]

def guard_runtime_imports(source: str, target_file: str, file_binding: ToolPoolFileBinding | dict[str, Any] | None = None) -> RuntimeImportGuardResult:
    allowed_helpers = set(_binding_values(file_binding, 'allowed_helper_imports', []))
    allowed_paths = set(_binding_values(file_binding, 'allowed_import_paths', []))
    allowed_functions = set(_binding_values(file_binding, 'allowed_function_imports', []))
    runtime_all = set(RUNTIME_TOOLS_ALL)
    missing: list[str] = []; forbidden: list[str] = []; warnings: list[str] = []
    custom_missing: list[str] = []; custom_forbidden: list[str] = []; custom_wildcard: list[str] = []
    try:
        tree = ast.parse(source or '')
    except SyntaxError as exc:
        return RuntimeImportGuardResult(success=False, error_type='generated_python_syntax_error', target_file=target_file, allowed_helper_imports=sorted(allowed_helpers), repair_instruction=str(exc))
    runtime_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if module == 'backend.services.runtime_tools':
                for alias in node.names:
                    name = alias.name
                    if name == '*': forbidden.append('*')
                    elif name not in runtime_all: missing.append(name)
                    elif name not in allowed_helpers: forbidden.append(name)
            elif module.startswith(_CUSTOM_PREFIX) or module in allowed_paths:
                if module not in allowed_paths:
                    custom_forbidden.append(module)
                for alias in node.names:
                    if alias.name == '*': custom_wildcard.append(module)
                    elif (alias.name not in allowed_functions and f'{module}.{alias.name}' not in allowed_functions) or module not in allowed_paths:
                        custom_forbidden.append(f'{module}.{alias.name}')
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == 'backend.services.runtime_tools': runtime_aliases.add(alias.asname or name.split('.')[-1])
                if name.startswith(_CUSTOM_PREFIX):
                    if name not in allowed_paths:
                        custom_forbidden.append(name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                if node.args and isinstance(node.args[0], ast.Constant):
                    imported=str(node.args[0].value)
                    if imported == 'backend.services.runtime_tools':
                        warnings.append('__import__ runtime_tools usage is not allowed for generated helpers'); forbidden.append('__import__(backend.services.runtime_tools)')
                    elif imported.startswith(_CUSTOM_PREFIX) and imported not in allowed_paths:
                        custom_forbidden.append(f'__import__({imported})')
            if isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in runtime_aliases and isinstance(node.args[1], ast.Constant):
                    name = str(node.args[1].value)
                    if name not in runtime_all: missing.append(name)
                    elif name not in allowed_helpers: forbidden.append(name)
    missing=sorted(set(missing)); forbidden=sorted(set(forbidden)); custom_forbidden=sorted(set(custom_forbidden)); custom_wildcard=sorted(set(custom_wildcard))
    if custom_wildcard:
        return RuntimeImportGuardResult(success=False, error_type='generated_custom_tool_wildcard_import', target_file=target_file, forbidden_imports=custom_wildcard, allowed_helper_imports=sorted(allowed_helpers), repair_instruction='Do not use wildcard imports for custom tools; use only allowed_import_paths and allowed_function_imports.')
    if custom_forbidden:
        return RuntimeImportGuardResult(success=False, error_type='generated_pool_forbidden_custom_tool_import', target_file=target_file, forbidden_imports=custom_forbidden, allowed_helper_imports=sorted(allowed_helpers), repair_instruction='Custom tool imports must match Current File Tool Binding allowed_import_paths and allowed_function_imports.')
    if missing or forbidden:
        err = 'generated_unknown_runtime_tool_import' if missing else 'generated_pool_forbidden_import'
        return RuntimeImportGuardResult(success=False, error_type=err, target_file=target_file, missing_imports=missing, forbidden_imports=forbidden, allowed_helper_imports=sorted(allowed_helpers), suggested_replacements=[], repair_instruction='Current import is not authorized by the Current File Tool Binding. Use only the listed allowed_helper_imports, allowed_import_paths, and allowed_function_imports. If the callable required by this FunctionItem is absent from the binding, report tool support insufficient; first-round ToolPool augmentation owns adding tools, and second-round repair must not search for or add tools. Do not bypass this with empty files, placeholders, fixed paths/strings, fake artifacts, or swallowed exceptions.', warnings=warnings)
    return RuntimeImportGuardResult(success=True, target_file=target_file, allowed_helper_imports=sorted(allowed_helpers), warnings=warnings)
