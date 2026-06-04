"""Blueprint parser — pure-Python extraction of file specs from a confirmed Skill blueprint.

No LLM calls are made here.  All regex failures degrade gracefully to
sensible defaults, and any uncertainty is captured in BlueprintPlan.warnings
so the frontend can surface it to the user.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .skill_plan import SkillPlan, SkillPlanEntry, build_skill_plan_entry

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileSpec:
    """A single file to be generated for a Skill package."""

    path: str          # relative to skill root: "SKILL.md" / "scripts/main.py"
    purpose: str       # human-readable description used as LLM prompt context
    required: bool = True
    can_skip: bool = False


@dataclass
class BlueprintPlan:
    """Parsed creation plan extracted from a confirmed Skill blueprint."""

    skill_name: str
    files: list[FileSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill_plan: SkillPlan | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLUEPRINT_MARKER = "📋 Skill 架构蓝图"

# "- **Skill 名称**: foo-bar" or "- **Skill 名称**: foo-bar"
_SKILL_NAME_RE = re.compile(
    r"-\s+\*\*Skill\s+名称\*\*[：:]\s*([^\n]+)",
    re.IGNORECASE,
)

# Inline backtick references: `scripts/main.py` or `references/guide.md`
_SKILL_PATH_INLINE_RE = re.compile(
    r"`((?:scripts|references|assets)/[^`\s]+)`"
)

# Tree structure patterns: 
# - ├── scripts/main.py 或 └── scripts/main.py (第一层级)
# - │   ├── scripts/main.py 或 │   └── scripts/main.py (第二层级)
# - /path/to/scripts/main.py (完整路径)
_TREE_FILE_RE = re.compile(
    r"(?:[│ ]{2,})?[├└]──\s*((?:/?[\w./-]+/)?(?:scripts|references|assets)/[^\s#]+)"
)

# "主入口脚本：scripts/xxx.py" or "主入口脚本: `scripts/xxx.py`"
_ENTRY_SCRIPT_RE = re.compile(
    r"主入口脚本[：:]\s*(`?)([^\n`]+)\1",
    re.IGNORECASE,
)

# "完整运行命令：python scripts/xxx.py ..."
_RUN_COMMAND_RE = re.compile(
    r"完整运行命令[：:]\s*([^\n]+)",
    re.IGNORECASE,
)

# Section lines in the blueprint for scripts / references / assets.
# These match lines like "- scripts/：..." or "- scripts/: 是否创建；..."
_SECTION_SCRIPTS_RE = re.compile(
    r"-\s+scripts/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)
_SECTION_REFERENCES_RE = re.compile(
    r"-\s+references/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)
_SECTION_ASSETS_RE = re.compile(
    r"-\s+assets/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)

# Phrases that indicate a section is not needed
_SKIP_PHRASES: tuple[str, ...] = (
    "无需创建",
    "无需",
    "不需要",
    "暂无",
    "none",
    "n/a",
)

# Maximum allowed length (chars) for a normalised Skill name.
_MAX_SKILL_NAME_LENGTH = 64

# Valid extensions per directory
_SCRIPT_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".js", ".ts", ".sh", ".bash", ".rb", ".mjs", ".cjs"}
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _should_skip(text: str) -> bool:
    """Return True when the section description says no files are needed."""
    stripped = text.strip().lower()
    # Exact "无" or starts with any skip phrase
    if stripped == "无":
        return True
    return any(stripped.startswith(phrase.lower()) for phrase in _SKIP_PHRASES)


def _contains_path_wildcard(path: str) -> bool:
    return any(ch in path for ch in "*?[]{}")


def _extract_inline_paths(text: str, prefix: str) -> list[str]:
    """Return all backtick-wrapped paths matching `prefix/...` found in text."""
    found: list[str] = []
    for m in _SKILL_PATH_INLINE_RE.finditer(text):
        p = m.group(1).strip()
        if p.startswith(prefix + "/") and p not in found:
            found.append(p)
    return found


# ---------------------------------------------------------------------------
# Public parsing functions
# ---------------------------------------------------------------------------


def extract_blueprint_text(messages: list[dict]) -> str | None:
    """Return the last assistant message containing the blueprint marker, or None."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content") or ""
            if _BLUEPRINT_MARKER in content:
                return content
    return None


def parse_skill_name(blueprint_text: str) -> str | None:
    """Extract and normalise the skill name from the blueprint.

    Returns a kebab-case identifier (lowercase letters, digits, hyphens),
    or None if the name cannot be reliably determined.
    """
    m = _SKILL_NAME_RE.search(blueprint_text)
    if not m:
        return None

    raw = m.group(1).strip()
    # Take only the first token (guard against trailing comments/parens)
    raw = raw.split()[0] if raw else ""

    # Normalise: lowercase, replace spaces/underscores with hyphens, keep alnum+hyphen
    normalised = re.sub(
        r"[^a-z0-9-]",
        "",
        raw.lower().replace(" ", "-").replace("_", "-"),
    )
    normalised = re.sub(r"-{2,}", "-", normalised).strip("-")

    if not normalised or len(normalised) > _MAX_SKILL_NAME_LENGTH:
        return None

    return normalised


def parse_files_from_blueprint(blueprint_text: str) -> tuple[list[FileSpec], list[str]]:
    """Extract the list of files to generate from the blueprint body.

    Returns (files, warnings).  All paths are relative to the skill root.
    """
    files: list[FileSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def _add(
        path: str,
        purpose: str,
        *,
        required: bool = True,
        can_skip: bool = False,
    ) -> None:
        if _contains_path_wildcard(path):
            warning = f"忽略通配符文件路径 {path}；Creator 只能逐个生成具体文件，请在蓝图中展开为具体文件名。"
            if warning not in warnings:
                warnings.append(warning)
            return
        if path in seen:
            return
        seen.add(path)
        files.append(
            FileSpec(path=path, purpose=purpose, required=required, can_skip=can_skip)
        )

    # 1. SKILL.md is always required
    _add("SKILL.md", "Skill 核心说明文件，包含 YAML frontmatter 和执行规范")

    # ------------------------------------------------------------------
    # 2. scripts/ section
    # ------------------------------------------------------------------
    scripts_desc = ""
    m_scripts = _SECTION_SCRIPTS_RE.search(blueprint_text)
    if m_scripts:
        scripts_desc = m_scripts.group(1).strip()

    # 3. Entry-point line (higher priority than section scan)
    m_entry = _ENTRY_SCRIPT_RE.search(blueprint_text)
    if m_entry:
        raw_entry = m_entry.group(2).strip().split()[0]  # first token
        if raw_entry and not _should_skip(raw_entry):
            if not raw_entry.startswith("scripts/"):
                raw_entry = "scripts/" + Path(raw_entry).name
            _add(raw_entry, scripts_desc or "Skill 主执行脚本")

    # 4. Inline backtick paths anywhere in the blueprint
    for path in _extract_inline_paths(blueprint_text, "scripts"):
        _add(path, scripts_desc or "Skill 执行脚本")

    # 4b. Tree structure paths (e.g., ├── scripts/main.py 或 ├── /path/to/scripts/main.py)
    for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
        tree_path = m_tree.group(1).strip()
        # 提取相对路径（移除前面的绝对路径部分）
        for prefix in ("scripts/", "references/", "assets/"):
            idx = tree_path.find(prefix)
            if idx >= 0:
                tree_path = tree_path[idx:]
                break
        if tree_path.startswith("scripts/"):
            _add(tree_path, scripts_desc or "Skill 执行脚本（从目录结构提取）")

    # 5. Bare paths inside the scripts section description
    if scripts_desc and not _should_skip(scripts_desc):
        for m_bare in re.finditer(r"scripts/(\S+\.\w+)", scripts_desc):
            _add("scripts/" + m_bare.group(1), scripts_desc)

    # 6. Infer default when section says files are needed but none identified
    if scripts_desc and not _should_skip(scripts_desc):
        has_script = any(f.path.startswith("scripts/") for f in files)
        if not has_script:
            default = "scripts/main.py"
            m_cmd = _RUN_COMMAND_RE.search(blueprint_text)
            if m_cmd:
                for token in m_cmd.group(1).split():
                    if token.startswith("scripts/") and Path(token).suffix in _SCRIPT_EXTENSIONS:
                        default = token
                        break
            _add(default, scripts_desc or "Skill 主执行脚本")
            warnings.append(
                f"脚本文件名未在蓝图中明确指定，已默认为 {default}，请在面板中确认或修改。"
            )

    # 7. Run-command line as additional source for script paths
    m_cmd = _RUN_COMMAND_RE.search(blueprint_text)
    if m_cmd:
        cmd_desc = scripts_desc or "Skill 主执行脚本（从运行命令推断）"
        for token in m_cmd.group(1).split():
            token = token.lstrip("./")
            if token.startswith("scripts/") and Path(token).suffix in _SCRIPT_EXTENSIONS:
                _add(token, cmd_desc)

    # ------------------------------------------------------------------
    # 8. references/ section
    # ------------------------------------------------------------------
    m_refs = _SECTION_REFERENCES_RE.search(blueprint_text)
    refs_desc = m_refs.group(1).strip() if m_refs else ""
    if refs_desc and not _should_skip(refs_desc):
        ref_files = _extract_inline_paths(blueprint_text, "references")
        for path in ref_files:
            _add(path, refs_desc, required=False, can_skip=True)
        # Tree structure paths for references
        for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
            tree_path = m_tree.group(1).strip()
            # 提取相对路径（移除前面的绝对路径部分）
            for prefix in ("scripts/", "references/", "assets/"):
                idx = tree_path.find(prefix)
                if idx >= 0:
                    tree_path = tree_path[idx:]
                    break
            if tree_path.startswith("references/"):
                _add(tree_path, refs_desc + "（从目录结构提取）", required=False, can_skip=True)
        # Bare filenames in section description
        for m_bare in re.finditer(r"references/(\S+\.\w+)", refs_desc):
            _add("references/" + m_bare.group(1), refs_desc, required=False, can_skip=True)
        if not any(f.path.startswith("references/") for f in files):
            default_ref = "references/guide.md"
            _add(default_ref, refs_desc, required=False, can_skip=True)
            warnings.append(
                f"参考资料文件名未在蓝图中明确指定，已默认为 {default_ref}，请在面板中确认或修改。"
            )

    # ------------------------------------------------------------------
    # 9. assets/ section
    # ------------------------------------------------------------------
    m_assets = _SECTION_ASSETS_RE.search(blueprint_text)
    assets_desc = m_assets.group(1).strip() if m_assets else ""
    if assets_desc and not _should_skip(assets_desc):
        asset_files = _extract_inline_paths(blueprint_text, "assets")
        for path in asset_files:
            _add(path, assets_desc, required=False, can_skip=True)
        # Tree structure paths for assets
        for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
            tree_path = m_tree.group(1).strip()
            # 提取相对路径（移除前面的绝对路径部分）
            for prefix in ("scripts/", "references/", "assets/"):
                idx = tree_path.find(prefix)
                if idx >= 0:
                    tree_path = tree_path[idx:]
                    break
            if tree_path.startswith("assets/"):
                _add(tree_path, assets_desc + "（从目录结构提取）", required=False, can_skip=True)
        for m_bare in re.finditer(r"assets/(\S+\.\w+)", assets_desc):
            _add("assets/" + m_bare.group(1), assets_desc, required=False, can_skip=True)
        if not any(f.path.startswith("assets/") for f in files):
            default_asset = "assets/template.md"
            _add(default_asset, assets_desc, required=False, can_skip=True)
            warnings.append(
                f"模板/资源文件名未在蓝图中明确指定，已默认为 {default_asset}，请在面板中确认或修改。"
            )

    return files, warnings


def build_skill_plan_from_files(
    *,
    skill_name: str,
    files: list[FileSpec],
    warnings: list[str] | None = None,
    blueprint_text: str = "",
) -> SkillPlan:
    """Build the role/contract plan used by Creator generation and validation."""
    reference_files = [file.path for file in files if file.path.startswith("references/")]
    entries: list[SkillPlanEntry] = []
    plan_warnings = list(warnings or [])
    for file in files:
        refs_for_file = reference_files if file.path == "SKILL.md" or file.path.startswith("scripts/") else []
        entry = build_skill_plan_entry(
            file_path=file.path,
            purpose=file.purpose,
            required=file.required,
            can_skip=file.can_skip,
            blueprint_summary=blueprint_text[:4000],
            reference_files=refs_for_file,
        )
        entries.append(entry)
        if file.path.startswith("scripts/") and entry.confidence < 0.7:
            plan_warnings.append(
                f"{file.path} 未声明明确 role，已使用保守 generic_script；"
                "不会自动启用图片生成/PDF 生成等高影响能力。"
            )
    return SkillPlan(skill_name=skill_name, files=entries, warnings=plan_warnings)


def parse_blueprint(messages: list[dict]) -> BlueprintPlan:
    """Parse a Skill blueprint from the conversation message history.

    Returns a BlueprintPlan with a best-effort file list and any warnings.
    If no blueprint is found, returns a minimal plan containing only SKILL.md.
    """
    blueprint_text = extract_blueprint_text(messages)
    if not blueprint_text:
        files = [FileSpec(path="SKILL.md", purpose="Skill 核心说明文件", required=True)]
        warnings = ["未在对话历史中找到蓝图，将创建最小 Skill 包（仅 SKILL.md）。"]
        return BlueprintPlan(
            skill_name="new-skill",
            files=files,
            warnings=warnings,
            skill_plan=build_skill_plan_from_files(
                skill_name="new-skill", files=files, warnings=warnings, blueprint_text=""
            ),
        )

    skill_name = parse_skill_name(blueprint_text)
    warnings: list[str] = []
    if skill_name is None:
        skill_name = "new-skill"
        warnings.append(
            "未能从蓝图中解析出合法 Skill 名称，已默认为 'new-skill'，请在面板中修改。"
        )

    files, file_warnings = parse_files_from_blueprint(blueprint_text)
    warnings.extend(file_warnings)

    skill_plan = build_skill_plan_from_files(
        skill_name=skill_name, files=files, warnings=warnings, blueprint_text=blueprint_text
    )
    return BlueprintPlan(
        skill_name=skill_name,
        files=files,
        warnings=skill_plan.warnings,
        skill_plan=skill_plan,
    )
