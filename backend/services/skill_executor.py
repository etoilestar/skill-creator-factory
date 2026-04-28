"""Skill Executor — runs kernel/scripts actions requested by the LLM.

Supported actions: init, write, validate, package.
All return a uniform dict: {action, name, success, message, path}.
"""

import importlib.util
import sys
from pathlib import Path

from ..config import settings

# Make kernel/scripts importable so package_skill can do `from quick_validate import …`
_SCRIPTS_DIR = str(settings.kernel_path / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _import_script(script_filename: str):
    """Dynamically import a script from kernel/scripts/.

    Uses sys.modules cache to avoid duplicate exec on repeated calls.
    """
    module_name = Path(script_filename).stem
    if module_name in sys.modules:
        return sys.modules[module_name]
    script_path = settings.kernel_path / "scripts" / script_filename
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_action(action: dict) -> dict:
    """Execute a single skill file-system action.

    Args:
        action: dict with at least {"action": str, "name": str} and optional fields.

    Returns:
        {"action": str, "name": str, "success": bool, "message": str, "path": str | None}
    """
    action_type = action.get("action", "")
    name = action.get("name", "").strip()

    if not name:
        return {
            "action": action_type,
            "name": name,
            "success": False,
            "message": "缺少 name 参数",
            "path": None,
        }

    skill_dir = settings.skills_path / name

    try:
        if action_type == "init":
            return _run_init(name, skill_dir)
        if action_type == "write":
            return _run_write(name, action.get("content", ""), skill_dir)
        if action_type == "validate":
            return _run_validate(name, skill_dir)
        if action_type == "package":
            return _run_package(name, skill_dir)
        return {
            "action": action_type,
            "name": name,
            "success": False,
            "message": f"未知动作类型: {action_type}",
            "path": None,
        }
    except Exception as exc:  # pragma: no cover
        return {
            "action": action_type,
            "name": name,
            "success": False,
            "message": f"执行出错: {exc}",
            "path": None,
        }


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def _run_init(name: str, skill_dir: Path) -> dict:
    if skill_dir.exists():
        return {
            "action": "init",
            "name": name,
            "success": True,
            "message": f"目录已存在，跳过初始化: {skill_dir.name}",
            "path": str(skill_dir),
        }
    mod = _import_script("init_skill.py")
    result = mod.init_skill(name, str(settings.skills_path))
    if result is None:
        return {
            "action": "init",
            "name": name,
            "success": False,
            "message": "初始化失败，请检查 skill 名称是否合法",
            "path": None,
        }
    return {
        "action": "init",
        "name": name,
        "success": True,
        "message": f"已创建 {name} 目录结构",
        "path": str(result),
    }


def _run_write(name: str, content: str, skill_dir: Path) -> dict:
    if not content:
        return {
            "action": "write",
            "name": name,
            "success": False,
            "message": "缺少 content 参数",
            "path": None,
        }
    from . import skill_manager  # local import to avoid circular deps

    skill_manager.save_skill(name, content)
    skill_md_path = skill_dir / "SKILL.md"
    return {
        "action": "write",
        "name": name,
        "success": True,
        "message": f"SKILL.md 已写入",
        "path": str(skill_md_path),
    }


def _run_validate(name: str, skill_dir: Path) -> dict:
    mod = _import_script("quick_validate.py")
    valid, message = mod.validate_skill(skill_dir)
    return {
        "action": "validate",
        "name": name,
        "success": valid,
        "message": message,
        "path": str(skill_dir / "SKILL.md") if valid else None,
    }


def _run_package(name: str, skill_dir: Path) -> dict:
    output_dir = skill_dir / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    mod = _import_script("package_skill.py")
    result = mod.package_skill(skill_dir, str(output_dir))
    if result is None:
        return {
            "action": "package",
            "name": name,
            "success": False,
            "message": "打包失败，请先执行 validate 确认 SKILL.md 格式正确",
            "path": None,
        }
    return {
        "action": "package",
        "name": name,
        "success": True,
        "message": f"已打包为 {name}.skill",
        "path": str(result),
    }
