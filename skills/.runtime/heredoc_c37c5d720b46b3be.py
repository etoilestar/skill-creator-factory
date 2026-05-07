from pathlib import Path
import re
import sys

root = Path(".agents/skills/get-current-time")
skill_md = root / "SKILL.md"
if not skill_md.exists():
    sys.stderr.write("错误：缺少 SKILL.md 文件\n")
    sys.exit(1)

text = skill_md.read_text(encoding="utf-8")
if not text.startswith("---\n"):
    sys.stderr.write("错误：SKILL.md 缺少 YAML frontmatter\n")
    sys.exit(1)

parts = text.split("---", 2)
if len(parts) < 3:
    sys.stderr.write("错误：SKILL.md frontmatter 未正确闭合\n")
    sys.exit(1)

frontmatter = parts[1]
name_match = re.search(r"^name:\s*([a-z0-9-]+)\s*$", frontmatter, re.M)
desc_match = re.search(r"^description:\s*(.+)\s*$", frontmatter, re.M)
if not name_match:
    sys.stderr.write("错误：frontmatter 缺少合法的 name 字段，只能使用小写字母、数字和连字符\n")
    sys.exit(1)
if len(name_match.group(1)) > 64:
    sys.stderr.write("错误：name 字段超过 64 个字符\n")
    sys.exit(1)
if not desc_match or not desc_match.group(1).strip():
    sys.stderr.write("错误：frontmatter 缺少 description 字段\n")
    sys.exit(1)

print("校验通过")
