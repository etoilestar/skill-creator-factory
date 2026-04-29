from pathlib import Path

from ..config import settings


def load_kernel_system_prompt() -> str:
    """Load kernel/SKILL.md as the skill-creator system prompt."""
    skill_md = settings.kernel_path / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Kernel SKILL.md not found at {skill_md}")
    return skill_md.read_text(encoding="utf-8")


def load_skill_system_prompt(skill_name: str) -> str:
    """Load a user skill's SKILL.md as the sandbox system prompt.

    If the skill has Python scripts in its scripts/ directory, an instruction
    block is appended so the model knows it can execute them via
    <skill_action> tags and will receive stdout/stderr back automatically.
    """
    skill_md = settings.skills_path / skill_name / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill '{skill_name}' not found")
    prompt = skill_md.read_text(encoding="utf-8")

    scripts_dir = settings.skills_path / skill_name / "scripts"
    scripts: list[str] = []
    if scripts_dir.is_dir():
        try:
            scripts = sorted(p.name for p in scripts_dir.iterdir() if p.is_file() and p.suffix == ".py")
        except OSError:
            pass

    if scripts:
        import json as _json
        script_list = "\n".join(f"- `{s}`" for s in scripts)
        example_json = _json.dumps(
            {"action": "run_script", "name": skill_name, "filename": "<脚本文件名>", "args": [], "stdin": ""},
            ensure_ascii=False,
        )
        tool_block = (
            "\n\n---\n\n"
            "## 沙盒工具：脚本执行\n\n"
            "当前沙盒中已预置以下 Python 脚本，你可以在回答用户问题时按需调用它们：\n\n"
            f"{script_list}\n\n"
            "### 调用方式\n\n"
            "在回复中输出一个 `<skill_action>` 标签，内容为 JSON：\n\n"
            "```\n"
            f"<skill_action>{example_json}</skill_action>\n"
            "```\n\n"
            "字段说明：\n\n"
            "| 字段 | 类型 | 说明 |\n"
            "|------|------|------|\n"
            "| `filename` | string | scripts/ 目录下的 `.py` 文件名 |\n"
            "| `args` | array | 命令行参数列表，可为空 `[]` |\n"
            "| `stdin` | string | 标准输入内容，可为空 `\"\"` |\n\n"
            "脚本执行后，后端会将 `stdout`、`stderr`、`exit_code` 自动注入到下一轮对话，"
            "你可以据此继续推理或向用户展示结果。\n\n"
            "**注意**：`<skill_action>` 标签内必须是合法 JSON，不得使用 Markdown 代码围栏包裹。"
        )
        prompt = prompt + tool_block

    return prompt
