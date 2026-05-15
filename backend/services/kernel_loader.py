from dataclasses import dataclass, field
from pathlib import Path
import re

import yaml

from ..config import settings
from .skill_manager import get_visible_skill_dir


@dataclass
class SkillResource:
    """A bundled resource inside a Skill package."""
    path: str
    kind: str
    title: str = ""

@dataclass
class ChildSkill:
    """A child Skill discovered under the current Skill package.

    只保存子 Skill 元数据，不保存正文。
    """
    ref: str
    name: str
    description: str
    root: Path

@dataclass
class SkillPackage:
    """A selected Skill package.

    分层说明：
    - metadata 阶段：只读取 frontmatter，不读取 SKILL.md 正文。
    - body 阶段：读取完整 SKILL.md。
    - child skill 阶段：先暴露子 Skill metadata，再按需加载子 Skill 正文。
    - resource 阶段：只在运行时明确需要时读取 references/assets/scripts。
    """
    name: str
    description: str
    root: Path
    skill_md_path: Path
    skill_md_text: str = ""
    references: list[SkillResource] = field(default_factory=list)
    assets: list[SkillResource] = field(default_factory=list)
    scripts: list[SkillResource] = field(default_factory=list)
    child_skills: list[ChildSkill] = field(default_factory=list)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

_ALLOWED_RESOURCE_DIRS = {"references", "assets", "scripts"}
_ALLOWED_READ_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".jinja",
    ".jinja2",
    ".template",
    ".tmpl",
    ".py",
}


def _parse_simple_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a SKILL.md string.

    Uses PyYAML for correct multi-line and nested value handling.  Falls back
    to an empty metadata dict on any parse error so that a malformed frontmatter
    never prevents loading the body.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    meta_text = match.group(1)
    body = match.group(2)

    try:
        meta = yaml.safe_load(meta_text)
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}

    return meta, body


def _read_frontmatter_only(skill_md_path: Path) -> tuple[dict[str, str], str]:
    """Read only frontmatter from SKILL.md.

    不读取完整正文，避免 metadata 阶段把完整 SKILL.md 加载进模型。
    """
    if not skill_md_path.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_md_path}")

    lines: list[str] = []
    delimiter_count = 0

    with skill_md_path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            lines.append(line)
            if line.strip() == "---":
                delimiter_count += 1
                if delimiter_count >= 2:
                    break

    frontmatter_text = "".join(lines)

    if delimiter_count < 2:
        preview = skill_md_path.read_text(encoding="utf-8", errors="replace")[:10000]
        return _parse_simple_frontmatter(preview)

    return _parse_simple_frontmatter(frontmatter_text)


def _extract_link_titles(skill_md_text: str) -> dict[str, str]:
    """Extract Markdown link titles from SKILL.md body."""
    result: dict[str, str] = {}

    for title, path in _MD_LINK_RE.findall(skill_md_text):
        normalized = path.strip().lstrip("./")
        result[normalized] = title.strip()

    return result


def _scan_resource_dir(
    root: Path,
    dirname: str,
    kind: str,
    link_titles: dict[str, str] | None = None,
) -> list[SkillResource]:
    """Scan bundled resources under a Skill directory.

    这里只扫描清单，不读取资源正文。
    """
    link_titles = link_titles or {}
    directory = root / dirname

    if not directory.is_dir():
        return []

    resources: list[SkillResource] = []

    try:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue

            rel_path = path.relative_to(root).as_posix()
            title = link_titles.get(rel_path, "")
            resources.append(SkillResource(path=rel_path, kind=kind, title=title))
    except OSError:
        return []

    return resources

def _is_skill_dir(path: Path) -> bool:
    """Return whether a directory looks like a Skill package."""
    return path.is_dir() and (path / "SKILL.md").is_file()


def _scan_child_skills(root: Path) -> list[ChildSkill]:
    """Scan child Skill packages without reading their bodies.

    支持两类布局：
    1. 当前 Skill 下的 skills/<child>/SKILL.md
    2. 当前 Skill 下的 references/<child>/SKILL.md

    注意：
    - 这里只读取子 Skill 的 frontmatter。
    - 不读取子 Skill 的 SKILL.md 正文。
    """
    candidates: list[Path] = []

    for dirname in ("skills", "references"):
        base = root / dirname
        if not base.is_dir():
            continue

        try:
            for path in sorted(base.rglob("SKILL.md")):
                parent = path.parent
                if _is_skill_dir(parent):
                    candidates.append(parent)
        except OSError:
            continue

    child_skills: list[ChildSkill] = []
    seen: set[str] = set()

    for child_root in candidates:
        try:
            rel = child_root.relative_to(root).as_posix()
        except ValueError:
            continue

        if rel in seen:
            continue
        seen.add(rel)

        try:
            meta, _body = _read_frontmatter_only(child_root / "SKILL.md")
        except Exception:
            continue

        name = meta.get("name") or child_root.name
        description = meta.get("description") or ""

        child_skills.append(
            ChildSkill(
                ref=rel,
                name=name,
                description=description,
                root=child_root,
            )
        )

    return child_skills

def _load_skill_from_root(root: Path, *, include_body: bool = False) -> SkillPackage:
    """Load a selected Skill package.

    include_body=False:
        只读取当前 Skill frontmatter + 扫描资源清单 + 扫描子 Skill metadata。

    include_body=True:
        读取当前 Skill 完整 SKILL.md 正文 + 扫描资源清单 + 扫描子 Skill metadata。

    注意：
    子 Skill 这里只读取 frontmatter，不读取正文。
    """
    skill_md = root / "SKILL.md"

    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_md}")

    if include_body:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        meta, _body = _parse_simple_frontmatter(text)
        link_titles = _extract_link_titles(text)
    else:
        meta, _body = _read_frontmatter_only(skill_md)
        text = ""
        link_titles = {}

    name = meta.get("name") or root.name
    description = meta.get("description") or ""

    return SkillPackage(
        name=name,
        description=description,
        root=root,
        skill_md_path=skill_md,
        skill_md_text=text,
        references=_scan_resource_dir(root, "references", "reference", link_titles),
        assets=_scan_resource_dir(root, "assets", "asset", link_titles),
        scripts=_scan_resource_dir(root, "scripts", "script", link_titles),
        child_skills=_scan_child_skills(root),
    )


def _format_resource_list(resources: list[SkillResource]) -> str:
    if not resources:
        return "- 无"

    lines: list[str] = []

    for resource in resources:
        if resource.title:
            lines.append(f"- `{resource.path}`：{resource.title}")
        else:
            lines.append(f"- `{resource.path}`")

    return "\n".join(lines)


def _compose_resource_manifest(skill: SkillPackage) -> str:
    """Build a resource manifest without reading resource bodies."""
    return (
        "## Bundled Resources Manifest\n\n"
        "以下只是当前 Skill 目录下的资源清单，不代表已经读取资源正文。\n"
        "references/assets/scripts 的具体内容只能在运行时确实需要时再读取或执行。\n"
        "如果 references 中包含子 Skill 索引，也只能先作为索引使用；不要默认加载所有子 Skill 正文。\n\n"
        "### references/\n"
        f"{_format_resource_list(skill.references)}\n\n"
        "### assets/\n"
        f"{_format_resource_list(skill.assets)}\n\n"
        "### scripts/\n"
        f"{_format_resource_list(skill.scripts)}"
    )

def _format_child_skill_list(child_skills: list[ChildSkill]) -> str:
    if not child_skills:
        return "- 无"

    lines: list[str] = []

    for child in child_skills:
        description = child.description.strip() or "无 description"
        lines.append(
            f"- ref: `{child.ref}`\n"
            f"  - name: `{child.name}`\n"
            f"  - description: {description}"
        )

    return "\n".join(lines)


def _compose_child_skill_manifest(skill: SkillPackage) -> str:
    """Build child Skill manifest without reading child bodies."""
    return (
        "## Child Skills Manifest\n\n"
        "以下是当前 Skill 下发现的子 Skill 清单。\n"
        "这里只提供子 Skill 的 frontmatter 元数据，不代表已经加载子 Skill 的 SKILL.md 正文。\n"
        "只有当用户请求确实匹配某个子 Skill 时，宿主才会按需加载该子 Skill 正文。\n\n"
        f"{_format_child_skill_list(skill.child_skills)}"
    )

def compose_metadata_prompt(skill: SkillPackage) -> str:
    """Compose metadata-only prompt for the first silent model round.

    第一阶段只用于判断该 Skill 是否需要进入正文执行阶段。
    同时暴露子 Skill metadata，但不读取子 Skill 正文。
    """
    return (
        "你处于 Skill 加载流程的第一阶段：metadata 判断阶段。\n\n"
        "当前只提供 Skill 的 frontmatter 元数据、资源清单和子 Skill 元数据，尚未加载当前 Skill 的 SKILL.md 正文，"
        "也尚未加载任何子 Skill 的 SKILL.md 正文。\n"
        "你需要根据用户请求判断是否应该加载当前 Skill 的完整 SKILL.md 正文。\n\n"
        "请只输出严格 JSON，不要输出解释文本，不要使用 Markdown。\n\n"
        "输出格式：\n"
        '{"need_body": true, "reason": "简短原因"}\n\n'
        "判断规则：\n"
        "- 如果用户请求符合当前 Skill description 中描述的使用场景，need_body=true。\n"
        "- 如果用户请求符合某个子 Skill 的 description，也应该 need_body=true，因为需要进入父 Skill 正文后再按需加载子 Skill。\n"
        "- 如果当前路由已经明确选中了该 Skill，通常 need_body=true。\n"
        "- 如果用户请求明显与当前 Skill 及其子 Skill 都无关，need_body=false。\n"
        "- metadata 阶段不要假装知道 SKILL.md 正文中的细节。\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def _compose_agent_runtime_contract() -> str:
    """Runtime contract injected into body stage.

    新版 contract：
    - SKILL.md 仍保持普通 Markdown。
    - 主执行路径由宿主 runtime planner 读取 Loaded SKILL.md 后生成结构化 action。
    - 主模型不再承担“必须吐 bash 代码块才能触发执行”的责任。
    """
    return (
        "## Host Agent Runtime Contract\n\n"
        "你运行在一个分层 Agent 宿主中，当前不是普通自由问答模式，而是 Skill 执行模式。\n\n"
        "核心原则：\n"
        "1. Loaded SKILL.md 是当前任务的最高执行规范，而不是普通参考资料。\n"
        "2. SKILL.md 可以保持标准 Markdown 写法，不需要包含自定义 action JSON 或 `<skill_action>` 标签。\n"
        "3. SKILL.md 中的 fenced code block 默认只是文档内容、代码内容、示例命令或使用方式，"
        "具体是否执行由宿主 runtime planner 根据语义判断。\n"
        "4. 如果 Loaded SKILL.md 语义上要求外部动作，例如运行脚本、执行命令、读取资源、写文件、创建目录、运行测试或调用工具，"
        "宿主会优先通过结构化 action plan 执行，而不是依赖你在最终回答里自由发挥。\n"
        "5. 你不得假装已经执行脚本、读取资源或生成文件；只有 executor observation 中出现的结果才是真实执行结果。\n\n"
        "动作与回答规则：\n"
        "1. 如果宿主已经执行了 action，你必须基于 observation 生成最终回答。\n"
        "2. 如果宿主没有执行 action，而 SKILL.md 又要求外部动作，你不得编造业务结果。\n"
        "3. 如果 SKILL.md 明确要求直接生成文本结果，或者 runtime planner 判定不需要外部动作，才可以直接回答。\n"
        "4. 如果缺少必要参数，应说明缺少哪些信息，不得保留占位符继续执行。\n\n"
        "资源限制：\n"
        "1. 不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主明确读取或执行后才可使用。\n"
        "2. 不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "3. 不要输出自定义 `<skill_action>` 标签。\n"
    )


def compose_body_prompt(skill: SkillPackage) -> str:
    """Compose body prompt for the second model round.

    第二阶段用于真正执行当前 Skill。
    子 Skill 仍然只提供 metadata，正文由宿主按需加载。
    """
    return (
        "你处于 Skill 加载流程的第二阶段：SKILL.md 正文执行阶段。\n\n"
        "请严格遵循下面完整 SKILL.md 中定义的流程、步骤、约束和输出要求。\n"
        "不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主运行时明确提供后才可使用。\n"
        "不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "不要输出自定义 `<skill_action>` 标签。\n\n"
        f"{_compose_agent_runtime_contract()}\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        "## Loaded SKILL.md\n\n"
        f"{skill.skill_md_text}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def load_kernel_package(*, include_body: bool = False) -> SkillPackage:
    """Load the fixed kernel Skill package."""
    return _load_skill_from_root(settings.kernel_path, include_body=include_body)


def load_user_skill_package(skill_name: str, *, include_body: bool = False) -> SkillPackage:
    """Load a user-created Skill package by directory name."""
    root = get_visible_skill_dir(skill_name, mode="sandbox")
    return _load_skill_from_root(root, include_body=include_body)


def load_kernel_metadata_prompt() -> str:
    """Load kernel Skill metadata-only prompt."""
    skill = load_kernel_package(include_body=False)
    return compose_metadata_prompt(skill)


def load_skill_metadata_prompt(skill_name: str) -> str:
    """Load selected Skill metadata-only prompt."""
    skill = load_user_skill_package(skill_name, include_body=False)
    return compose_metadata_prompt(skill)


def load_kernel_body_prompt() -> str:
    """Load kernel Skill full SKILL.md body prompt."""
    skill = load_kernel_package(include_body=True)
    return compose_body_prompt(skill)


def load_skill_body_prompt(skill_name: str) -> str:
    """Load selected Skill full SKILL.md body prompt."""
    skill = load_user_skill_package(skill_name, include_body=True)
    return compose_body_prompt(skill)

def resolve_child_skill_root(parent_skill_name: str, child_ref: str) -> Path:
    """Resolve a child Skill root under a parent Skill.

    child_ref 必须来自 Child Skills Manifest 中的 ref。
    """
    if not child_ref or "\x00" in child_ref:
        raise ValueError("子 Skill 引用非法")

    if child_ref.startswith("/") or "\\" in child_ref:
        raise ValueError("子 Skill 引用不允许使用绝对路径或反斜杠")

    rel = Path(child_ref)

    if any(part in {"", ".."} for part in rel.parts):
        raise ValueError("子 Skill 引用路径越界")

    parent = _select_skill_for_resource_action(parent_skill_name)
    child_root = (parent.root / rel).resolve()
    parent_root = parent.root.resolve()

    try:
        child_root.relative_to(parent_root)
    except ValueError as exc:
        raise ValueError("子 Skill 路径越界") from exc

    if not _is_skill_dir(child_root):
        raise FileNotFoundError(f"子 Skill 不存在: {child_ref}")

    return child_root


def load_child_skill_metadata_prompt(parent_skill_name: str, child_ref: str) -> str:
    """Load child Skill metadata-only prompt."""
    child_root = resolve_child_skill_root(parent_skill_name, child_ref)
    child = _load_skill_from_root(child_root, include_body=False)
    return compose_metadata_prompt(child)


def load_child_skill_body_prompt(parent_skill_name: str, child_ref: str) -> str:
    """Load child Skill full body prompt."""
    child_root = resolve_child_skill_root(parent_skill_name, child_ref)
    child = _load_skill_from_root(child_root, include_body=True)

    return (
        "你现在额外加载了一个子 Skill 的完整正文。\n"
        "该子 Skill 只用于当前用户请求中与其 description 匹配的部分。\n"
        "若父 Skill 与子 Skill 规则冲突，优先遵循更具体的子 Skill；若涉及宿主安全限制，始终遵循宿主安全限制。\n\n"
        f"{compose_body_prompt(child)}"
    )

# 兼容旧函数名：不要在新代码里优先使用这两个名字。
def load_kernel_system_prompt() -> str:
    """Compatibility alias: metadata-only prompt, not full SKILL.md."""
    return load_kernel_metadata_prompt()


def load_skill_system_prompt(skill_name: str) -> str:
    """Compatibility alias: metadata-only prompt, not full SKILL.md."""
    return load_skill_metadata_prompt(skill_name)


def resolve_skill_resource(skill: SkillPackage, rel_path: str) -> Path:
    """Resolve and validate a resource path inside the Skill directory."""
    if not rel_path or "\x00" in rel_path:
        raise ValueError("资源路径非法")

    rel = Path(rel_path)

    if rel.is_absolute():
        raise ValueError("不允许读取绝对路径")

    parts = rel.parts

    if not parts:
        raise ValueError("资源路径为空")

    if parts[0] not in _ALLOWED_RESOURCE_DIRS:
        raise ValueError("只允许读取 references/、assets/、scripts/ 下的资源")

    resolved_root = skill.root.resolve()
    resolved_path = (skill.root / rel).resolve()

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("资源路径越界") from exc

    if not resolved_path.exists() or not resolved_path.is_file():
        raise FileNotFoundError(f"资源不存在: {rel_path}")

    suffix = resolved_path.suffix.lower()

    if suffix not in _ALLOWED_READ_SUFFIXES:
        raise ValueError(f"不允许直接读取该类型资源: {suffix}")

    return resolved_path


def _select_skill_for_resource_action(skill_name: str) -> SkillPackage:
    """Select kernel or user Skill for resource reading."""
    kernel = load_kernel_package(include_body=False)

    if skill_name in {kernel.name, "kernel", "skill-creator"}:
        return load_kernel_package(include_body=False)

    return load_user_skill_package(skill_name, include_body=False)


def read_skill_resource_text(
    skill_name: str,
    rel_path: str,
    *,
    max_chars: int = 20000,
) -> dict:
    """Read a bundled Skill resource safely.

    这里只提供底层能力；是否读取由上层运行时决定。
    """
    skill = _select_skill_for_resource_action(skill_name)
    path = resolve_skill_resource(skill, rel_path)

    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = False

    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {
        "action": "read_skill_resource",
        "name": skill.name,
        "path": rel_path,
        "success": True,
        "content": text,
        "truncated": truncated,
    }
