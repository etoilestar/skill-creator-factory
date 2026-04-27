"""Generate skill folder from collected session data."""
import shutil
from pathlib import Path

from .config import KERNEL_PATH, SKILL_DATA_PATH


def generate_skill_folder(skill_data: dict) -> str:
    """Create skill-data/{name}/ with SKILL.md and subdirectories.
    
    Args:
        skill_data: must contain at minimum 'name' and 'description'.
    
    Returns:
        Absolute path string of the generated skill directory.
    """
    name = skill_data.get("name", "").strip()
    if not name:
        raise ValueError("skill_data must contain a non-empty 'name'")

    description = skill_data.get("description", "")
    trigger = skill_data.get("trigger_words", skill_data.get("trigger", ""))
    input_fmt = skill_data.get("input_format", skill_data.get("input", ""))
    output_fmt = skill_data.get("output_format", skill_data.get("output", ""))

    skill_dir = Path(SKILL_DATA_PATH) / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Create standard subdirectories
    for sub in ("scripts", "references", "assets"):
        (skill_dir / sub).mkdir(exist_ok=True)

    # Copy scripts from kernel (read-only source)
    kernel_scripts = Path(KERNEL_PATH) / "scripts"
    if kernel_scripts.exists():
        for script in kernel_scripts.iterdir():
            if script.is_file():
                dest = skill_dir / "scripts" / script.name
                if not dest.exists():
                    shutil.copy2(script, dest)

    # Copy reference files from kernel
    kernel_refs = Path(KERNEL_PATH) / "references"
    if kernel_refs.exists():
        for ref in kernel_refs.iterdir():
            if ref.is_file():
                dest = skill_dir / "references" / ref.name
                if not dest.exists():
                    shutil.copy2(ref, dest)

    # Build SKILL.md content
    skill_md_content = _build_skill_md(
        name=name,
        description=description,
        trigger=trigger,
        input_fmt=input_fmt,
        output_fmt=output_fmt,
        extra=skill_data,
    )
    (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")

    return str(skill_dir.resolve())


def _build_skill_md(name: str, description: str, trigger: str,
                    input_fmt: str, output_fmt: str, extra: dict) -> str:
    """Compose a SKILL.md with standard YAML frontmatter."""
    # Escape description for YAML (replace newlines, quote if needed)
    desc_yaml = description.replace('"', '\\"').replace("\n", " ")
    trigger_yaml = trigger.replace('"', '\\"').replace("\n", " ")

    lines = [
        "---",
        f'name: {name}',
        f'description: "{desc_yaml}"',
        "---",
        "",
        f"# {name}",
        "",
    ]

    if trigger:
        lines += [
            "## 触发场景",
            "",
            trigger,
            "",
        ]

    if input_fmt:
        lines += [
            "## 输入",
            "",
            input_fmt,
            "",
        ]

    if output_fmt:
        lines += [
            "## 输出",
            "",
            output_fmt,
            "",
        ]

    # Append any other collected fields as notes
    skip = {"name", "description", "trigger_words", "trigger",
             "input_format", "input", "output_format", "output"}
    extras = {k: v for k, v in extra.items() if k not in skip and v}
    if extras:
        lines += ["## 备注", ""]
        for k, v in extras.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")

    return "\n".join(lines)
