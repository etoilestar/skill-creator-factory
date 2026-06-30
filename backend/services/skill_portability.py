"""Portable Skill ZIP dependency collection and runtime bundling."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

PORTABILITY_VERSION = "1.1"
RUNTIME_DIR = "_portable_runtime"
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_TOOLS_DIR = REPO_ROOT / "backend" / "services" / "runtime_tools"
SKILL_RUNTIME_FILE = REPO_ROOT / "backend" / "services" / "skill_runtime.py"

DEFAULT_ENV_PLACEHOLDERS = {
    "OUTPUT_DIR": "./outputs",
    "INPUT_DIR": "./inputs",
    "LLM_API_BASE": "<FILL_ME>",
    "LLM_API_KEY": "<FILL_ME>",
    "LLM_MODEL": "<FILL_ME>",
    "IMAGE_API_BASE": "<FILL_ME>",
    "IMAGE_API_KEY": "<FILL_ME>",
}
_ENV_ALIASES = {
    "LLM_BASE_URL": "LLM_API_BASE",
    "TEXT_MODEL": "LLM_MODEL",
    "IMAGE_BASE_URL": "IMAGE_API_BASE",
    "IMAGE_MODEL": "IMAGE_MODEL",
}
SERVICE_ENV_BY_TOOL = {
    "generate_text_with_llm": {"LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"},
    "generate_image": {"IMAGE_API_BASE", "IMAGE_API_KEY", "IMAGE_MODEL"},
    "generate_stable_diffusion_image": {"IMAGE_API_BASE", "IMAGE_API_KEY", "IMAGE_MODEL", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"},
    "web_search": {"SEARCHXNG_BASE_URL", "SEARCHXNG_API_KEY", "SEARCHXNG_ENGINE", "SEARCHXNG_TIMEOUT"},
    "query_database_readonly": {"DATABASE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"},
    "describe_database_table": {"DATABASE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"},
    "list_database_tables": {"DATABASE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"},
    "registered_tool_call": {"REGISTERED_TOOL_API_BASE", "REGISTERED_TOOL_API_KEY"},
    "create_wechat_draft": {"WECHAT_APP_ID", "WECHAT_APP_SECRET"},
    "publish_wechat_draft": {"WECHAT_APP_ID", "WECHAT_APP_SECRET"},
    "upload_wechat_media": {"WECHAT_APP_ID", "WECHAT_APP_SECRET"},
}


@dataclass
class PortableReport:
    system_tools_used: list[str] = field(default_factory=list)
    copied_modules: list[str] = field(default_factory=list)
    copied_runtime_files: list[str] = field(default_factory=list)
    third_party_imports: list[str] = field(default_factory=list)
    third_party_requirements: list[str] = field(default_factory=list)
    unresolved_requirements: list[str] = field(default_factory=list)
    env_vars_used: list[str] = field(default_factory=list)
    env_placeholders: dict[str, str] = field(default_factory=dict)
    patched_scripts: list[str] = field(default_factory=list)
    patched_imports: list[dict[str, str]] = field(default_factory=list)
    adapted_tools: list[str] = field(default_factory=list)
    unsupported_tools: list[str] = field(default_factory=list)
    missing_runtime_modules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_runtime_tools: list[str] = field(default_factory=list)
    used_skill_runtime_tools: list[str] = field(default_factory=list)

    def manifest(self) -> dict[str, Any]:
        return {
            "portability_version": PORTABILITY_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runtime_bundle_dir": RUNTIME_DIR,
            "system_tools_used": self.system_tools_used,
            "patched_imports": self.patched_imports,
            "copied_modules": self.copied_modules,
            "copied_runtime_files": self.copied_runtime_files,
            "adapted_tools": self.adapted_tools,
            "unsupported_tools": self.unsupported_tools,
            "missing_runtime_modules": self.missing_runtime_modules,
            "third_party_requirements": self.third_party_requirements,
            "unresolved_requirements": self.unresolved_requirements,
            "env_vars_used": self.env_vars_used,
            "env_placeholders": self.env_placeholders,
            "requirements_file": "requirements-portable.txt",
            "env_example_file": ".env.example",
            "warnings": self.warnings,
            "dependency_report": {
                "third_party_imports": self.third_party_imports,
                "patched_scripts": self.patched_scripts,
            },
        }


def _stdlib_roots() -> set[str]:
    return set(getattr(sys, "stdlib_module_names", set())) | {"__future__", "typing"}


def _local_module_roots(skill_dir: Path) -> set[str]:
    roots = {RUNTIME_DIR, "_portable_runtime"}
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for path in scripts_dir.rglob("*.py"):
            roots.add(path.stem)
            rel = path.relative_to(scripts_dir).with_suffix("")
            if rel.parts:
                roots.add(rel.parts[0])
    return roots


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def _runtime_export_map() -> dict[str, str]:
    tree = _parse(RUNTIME_TOOLS_DIR / "__init__.py")
    mapping: dict[str, str] = {}
    if tree is None:
        return mapping
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            module = f"backend.services.runtime_tools.{node.module}"
            for alias in node.names:
                mapping[alias.asname or alias.name] = module
    return mapping


def _script_system_imports(skill_dir: Path) -> tuple[set[str], set[str], set[str], set[str], list[tuple[Path, ast.Module]]]:
    runtime_names: set[str] = set()
    runtime_modules: set[str] = set()
    skill_runtime_names: set[str] = set()
    third_party_roots: set[str] = set()
    parsed_scripts: list[tuple[Path, ast.Module]] = []
    local_roots = _local_module_roots(skill_dir)
    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        tree = _parse(script)
        if tree is None:
            continue
        parsed_scripts.append((script, tree))
        for root in _import_roots(tree):
            if _is_third_party(root, local_roots):
                third_party_roots.add(root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module or node.level != 0:
                continue
            if node.module == "backend.services.runtime_tools":
                runtime_names.update(alias.name for alias in node.names if alias.name != "*")
            elif node.module.startswith("backend.services.runtime_tools."):
                runtime_modules.add(node.module)
                runtime_names.update(alias.name for alias in node.names if alias.name != "*")
            elif node.module == "backend.services.skill_runtime":
                skill_runtime_names.update(alias.name for alias in node.names if alias.name != "*")
    return runtime_names, runtime_modules, skill_runtime_names, third_party_roots, parsed_scripts


def _runtime_module_file(module: str) -> Path | None:
    prefix = "backend.services.runtime_tools."
    if not module.startswith(prefix):
        return None
    rel = module.removeprefix(prefix).replace(".", "/") + ".py"
    path = RUNTIME_TOOLS_DIR / rel
    if path.is_file() and path.resolve().is_relative_to(RUNTIME_TOOLS_DIR.resolve()):
        return path
    return None


def _module_name_for_file(path: Path) -> str:
    if path == SKILL_RUNTIME_FILE:
        return "backend.services.skill_runtime"
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _discover_runtime_closure(runtime_names: set[str], runtime_modules: set[str], skill_runtime_names: set[str], report: PortableReport) -> set[Path]:
    export_map = _runtime_export_map()
    files: set[Path] = set()
    queue: list[Path] = []

    def add(path: Path | None, module_name: str | None = None):
        if path is None:
            if module_name:
                report.missing_runtime_modules.append(module_name)
            return
        if path not in files:
            files.add(path)
            queue.append(path)

    for module in sorted(runtime_modules):
        add(_runtime_module_file(module), module)
    for name in sorted(runtime_names):
        add(_runtime_module_file(export_map.get(name, "")), name)
    if skill_runtime_names:
        add(SKILL_RUNTIME_FILE, "backend.services.skill_runtime")
        # skill_runtime imports the runtime_tools facade broadly; copy direct runtime modules so imports remain real.
        for path in sorted(RUNTIME_TOOLS_DIR.glob("*.py")):
            if path.name != "__init__.py":
                add(path, _module_name_for_file(path))
    while queue:
        path = queue.pop(0)
        tree = _parse(path)
        if tree is None:
            report.warnings.append(f"Could not parse runtime module {path}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1 and path.parent == RUNTIME_TOOLS_DIR and node.module:
                    add(RUNTIME_TOOLS_DIR / f"{node.module}.py", f"backend.services.runtime_tools.{node.module}")
                elif node.level == 1 and path == SKILL_RUNTIME_FILE and node.module == "runtime_tools":
                    for dep in sorted(RUNTIME_TOOLS_DIR.glob("*.py")):
                        if dep.name != "__init__.py":
                            add(dep, _module_name_for_file(dep))
                elif node.module and node.module.startswith("backend.services.runtime_tools."):
                    add(_runtime_module_file(node.module), node.module)
    return files


def _import_roots(tree: ast.AST) -> list[str]:
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.append(node.module.split(".")[0])
    return roots


def _env_vars(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr in {"get", "getenv"} and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
            elif isinstance(func, ast.Name) and func.id in {"_env", "_required_env"} and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
            if name:
                found.add(_ENV_ALIASES.get(name, name))
        elif isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                found.add(_ENV_ALIASES.get(node.slice.value, node.slice.value))
    return found


def _is_third_party(root: str, local_roots: set[str]) -> bool:
    return bool(root and root not in local_roots and root != "backend" and root not in _stdlib_roots())


def _requirement_for_import(root: str) -> tuple[str, bool]:
    try:
        dists = importlib.metadata.packages_distributions().get(root) or []
        if dists:
            dist = dists[0]
            return f"{dist}=={importlib.metadata.version(dist)}", True
    except Exception:
        pass
    return root, False


def _placeholders(envs: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in sorted(envs):
        result[name] = DEFAULT_ENV_PLACEHOLDERS.get(name, "<FILL_ME>")
    return result


def collect_portable_dependencies(skill_dir: str | Path) -> PortableReport:
    skill_dir = Path(skill_dir)
    report = PortableReport()
    runtime_names, runtime_modules, skill_runtime_names, third_party, parsed_scripts = _script_system_imports(skill_dir)
    copied_files = _discover_runtime_closure(runtime_names, runtime_modules, skill_runtime_names, report)
    envs = set(DEFAULT_ENV_PLACEHOLDERS)
    local_roots = _local_module_roots(skill_dir)

    for _script, tree in parsed_scripts:
        envs.update(_env_vars(tree))
    for path in copied_files:
        tree = _parse(path)
        if tree is None:
            continue
        envs.update(_env_vars(tree))
        for root in _import_roots(tree):
            if _is_third_party(root, local_roots):
                third_party.add(root)

    used = sorted(runtime_names | skill_runtime_names)
    report.used_runtime_tools = sorted(runtime_names)
    report.used_skill_runtime_tools = sorted(skill_runtime_names)
    report.system_tools_used = [f"backend.services.runtime_tools:{name}" for name in sorted(runtime_names)]
    if skill_runtime_names:
        report.system_tools_used.extend(f"backend.services.skill_runtime:{name}" for name in sorted(skill_runtime_names))
    adapted = sorted(name for name in used if name in SERVICE_ENV_BY_TOOL)
    report.adapted_tools = adapted
    for name in adapted:
        envs.update(SERVICE_ENV_BY_TOOL[name])
    report.copied_modules = sorted(_module_name_for_file(p) for p in copied_files)
    report.copied_runtime_files = sorted(_portable_arc_for_source(p) for p in copied_files)
    # package skeleton/facades
    report.copied_runtime_files.extend([
        f"{RUNTIME_DIR}/__init__.py",
        f"{RUNTIME_DIR}/backend/__init__.py",
        f"{RUNTIME_DIR}/backend/services/__init__.py",
        f"{RUNTIME_DIR}/backend/services/runtime_tools/__init__.py",
        f"{RUNTIME_DIR}/runtime_tools.py",
        f"{RUNTIME_DIR}/skill_runtime.py",
        f"{RUNTIME_DIR}/env.py",
        f"{RUNTIME_DIR}/bootstrap.py",
    ])
    report.copied_runtime_files = sorted(set(report.copied_runtime_files))
    report.third_party_imports = sorted(third_party)
    seen_req: set[str] = set()
    for root in report.third_party_imports:
        req, resolved = _requirement_for_import(root)
        if req not in seen_req:
            seen_req.add(req)
            report.third_party_requirements.append(req)
        if not resolved:
            report.unresolved_requirements.append(root)
    report.env_placeholders = _placeholders(envs)
    report.env_vars_used = sorted(report.env_placeholders)
    return report


def patch_script_imports(source: str, rel_path: str, report: PortableReport) -> str:
    tree = ast.parse(source)
    lines = source.splitlines()
    replacements: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module or node.level != 0:
            continue
        target = None
        if node.module.startswith("_portable_runtime"):
            continue
        if node.module == "backend.services.runtime_tools" or node.module.startswith("backend.services.runtime_tools."):
            target = "_portable_runtime.backend.services.runtime_tools"
        elif node.module == "backend.services.skill_runtime":
            target = "_portable_runtime.backend.services.skill_runtime"
        if not target:
            continue
        imported = ", ".join(alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in node.names)
        fallback = f"try:\n    from {target} import {imported}\nexcept ImportError:\n    from {node.module} import {imported}"
        replacements.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno), fallback))
        report.patched_imports.append({"script": rel_path, "from": node.module, "to": target})
    if not replacements:
        return source
    for start, end, text in sorted(replacements, reverse=True):
        lines[start:end] = text.splitlines()
    if rel_path not in report.patched_scripts:
        report.patched_scripts.append(rel_path)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _portable_arc_for_source(path: Path) -> str:
    if path == SKILL_RUNTIME_FILE:
        return f"{RUNTIME_DIR}/backend/services/skill_runtime.py"
    rel = path.relative_to(RUNTIME_TOOLS_DIR)
    return f"{RUNTIME_DIR}/backend/services/runtime_tools/{rel.as_posix()}"


def _runtime_init(report: PortableReport) -> str:
    export_map = _runtime_export_map()
    by_module: dict[str, list[str]] = {}
    if "backend.services.skill_runtime" in report.copied_modules:
        # Full facade needed by copied skill_runtime.py.
        return (RUNTIME_TOOLS_DIR / "__init__.py").read_text(encoding="utf-8")
    for name in report.used_runtime_tools:
        module = export_map.get(name)
        if module:
            by_module.setdefault(module.rsplit(".", 1)[-1], []).append(name)
    lines = ['"""Portable runtime_tools facade re-exporting copied real implementations."""', ""]
    all_names: list[str] = []
    for module, names in sorted(by_module.items()):
        joined = ", ".join(sorted(names))
        lines.append(f"from .{module} import {joined}")
        all_names.extend(names)
    lines.append("")
    lines.append(f"__all__ = {sorted(all_names)!r}")
    lines.append("")
    return "\n".join(lines)


def _top_facade(module: str, names: list[str]) -> str:
    if not names:
        return f'"""Portable {module} facade."""\n'
    joined = ", ".join(sorted(names))
    return f'"""Portable {module} facade."""\nfrom .backend.services.{module} import {joined}\n__all__ = {sorted(names)!r}\n'


def _portable_skill_runtime_facade(report: PortableReport) -> str:
    return _top_facade("skill_runtime", report.used_skill_runtime_tools)


def _portable_runtime_tools_facade(report: PortableReport) -> str:
    return _top_facade("runtime_tools", report.used_runtime_tools)


def _patched_skill_runtime_source() -> str:
    source = SKILL_RUNTIME_FILE.read_text(encoding="utf-8")
    aliases = """
def _required_env(name: str) -> str:
    alias_values = {
        "LLM_BASE_URL": ("LLM_BASE_URL", "LLM_API_BASE"),
        "TEXT_MODEL": ("TEXT_MODEL", "LLM_MODEL"),
        "IMAGE_BASE_URL": ("IMAGE_BASE_URL", "IMAGE_API_BASE"),
    }
    names = alias_values.get(name, (name,))
    for candidate in names:
        value = _env(candidate).strip()
        if value and value != "<FILL_ME>":
            return value
    raise RuntimeError(
        f"Portable Skill runtime requires environment variable {name}"
        + (f" (or {', '.join(names[1:])})" if len(names) > 1 else "")
        + "; fill .env.example placeholders or configure a host adapter."
    )
""".strip()
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_required_env":
            lines[node.lineno - 1:getattr(node, "end_lineno", node.lineno)] = aliases.splitlines()
            break
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def portable_runtime_files(report: PortableReport) -> dict[str, str]:
    files = {
        f"{RUNTIME_DIR}/__init__.py": '"""Portable runtime bundled with an exported Skill."""\n',
        f"{RUNTIME_DIR}/backend/__init__.py": "\n",
        f"{RUNTIME_DIR}/backend/services/__init__.py": "\n",
        f"{RUNTIME_DIR}/backend/services/runtime_tools/__init__.py": _runtime_init(report),
        f"{RUNTIME_DIR}/runtime_tools.py": _portable_runtime_tools_facade(report),
        f"{RUNTIME_DIR}/skill_runtime.py": _portable_skill_runtime_facade(report),
        f"{RUNTIME_DIR}/env.py": "from __future__ import annotations\nimport os\ndef get(name, default=''):\n    return os.environ.get(name, default)\n",
        f"{RUNTIME_DIR}/bootstrap.py": "from __future__ import annotations\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n",
    }
    for module in report.copied_modules:
        if module == "backend.services.skill_runtime":
            files[f"{RUNTIME_DIR}/backend/services/skill_runtime.py"] = _patched_skill_runtime_source()
            continue
        if module.startswith("backend.services.runtime_tools."):
            name = module.rsplit(".", 1)[-1]
            src = RUNTIME_TOOLS_DIR / f"{name}.py"
            if src.is_file():
                files[f"{RUNTIME_DIR}/backend/services/runtime_tools/{name}.py"] = src.read_text(encoding="utf-8")
    return files


def env_example(report: PortableReport) -> str:
    return "".join(f"{name}={value}\n" for name, value in sorted(report.env_placeholders.items()))


def add_portable_files_to_zip(zipf: ZipFile, skill_dir: Path, arc_prefix: str = "") -> PortableReport:
    report = collect_portable_dependencies(skill_dir)
    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        rel = script.relative_to(skill_dir).as_posix()
        patched = patch_script_imports(script.read_text(encoding="utf-8"), rel, report)
        zipf.writestr(f"{arc_prefix}{rel}", patched)
    # Recompute requirements after generated facades/adapters are available.
    for content in portable_runtime_files(report).values():
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        for root in _import_roots(tree):
            if _is_third_party(root, {RUNTIME_DIR, "_portable_runtime", "backend"}) and root not in report.third_party_imports:
                report.third_party_imports.append(root)
                req, resolved = _requirement_for_import(root)
                if req not in report.third_party_requirements:
                    report.third_party_requirements.append(req)
                if not resolved:
                    report.unresolved_requirements.append(root)
    runtime_files = portable_runtime_files(report)
    for arc, content in runtime_files.items():
        zipf.writestr(f"{arc_prefix}{arc}", content)
    zipf.writestr(f"{arc_prefix}requirements-portable.txt", "\n".join(report.third_party_requirements) + ("\n" if report.third_party_requirements else ""))
    zipf.writestr(f"{arc_prefix}.env.example", env_example(report))
    zipf.writestr(f"{arc_prefix}skill-portability.json", json.dumps(report.manifest(), ensure_ascii=False, indent=2) + "\n")
    return report
