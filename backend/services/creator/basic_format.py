"""Coarse post-patch basic format checks for Creator candidates.

These checks intentionally validate only file/source shape. They do not import,
execute, inspect tool bindings, validate argv/stdout contracts, or run E2E.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BasicFormatFailure:
    coarse_failure_kind: str
    message: str
    instruction: str

    def to_failure_text(self) -> str:
        return (
            f"coarse_failure_kind={self.coarse_failure_kind}\n"
            f"message={self.message}\n"
            f"instruction={self.instruction}"
        )


PYTHON_BASIC_FAILURE = BasicFormatFailure(
    coarse_failure_kind="python_compile_error",
    message="patch 后候选 Python 源码无法通过基础编译/语法校验",
    instruction="只修复当前文件基础源码格式，不要同时修改工具、argv、stdout、E2E 或业务逻辑",
)

MARKDOWN_BASIC_FAILURE = BasicFormatFailure(
    coarse_failure_kind="markdown_basic_format_error",
    message="patch 后候选 Markdown 基础格式被破坏",
    instruction="只修复 Markdown/frontmatter/fence 基础格式，不处理 E2E 语义",
)

_SCRIPT_EXPLANATION_RE = re.compile(r"(?im)^\s*(?:here is|下面是|以下是|解释|说明|note:|注意[:：])")
_UNIFIED_DIFF_RE = re.compile(r"(?m)^\s*(?:diff --git |@@ |---\s+\S+\s*$|\+\+\+\s+\S+\s*$)")
_JSON_PATCH_RE = re.compile(r'"(?:op|path|old_lines|new_lines)"\s*:')


def _balanced_markdown_fences(text: str) -> bool:
    stack: list[tuple[str, int]] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if not match:
            continue
        marker = match.group(1)
        char = marker[0]
        length = len(marker)
        if stack and stack[-1][0] == char and length >= stack[-1][1]:
            stack.pop()
        else:
            stack.append((char, length))
    return not stack


def _frontmatter_closed(text: str) -> bool:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return True
    return any(line.strip() == "---" for line in lines[1:])


def check_patch_candidate_basic_format(file_path: str, content: str) -> BasicFormatFailure | None:
    """Return a coarse basic-format failure, or None when the candidate is shape-valid."""
    text = str(content or "")
    stripped = text.strip().lstrip("\ufeff")
    if not stripped:
        return PYTHON_BASIC_FAILURE if file_path.startswith("scripts/") and file_path.endswith(".py") else MARKDOWN_BASIC_FAILURE

    if file_path.startswith("scripts/") and file_path.endswith(".py"):
        if re.fullmatch(r"(```|~~~)[^\n`~]*\n[\s\S]*\n\1\s*", stripped):
            return PYTHON_BASIC_FAILURE
        if _UNIFIED_DIFF_RE.search(stripped) or _JSON_PATCH_RE.search(stripped) or _SCRIPT_EXPLANATION_RE.search(stripped):
            return PYTHON_BASIC_FAILURE
        try:
            ast.parse(text, filename=file_path)
        except SyntaxError:
            return PYTHON_BASIC_FAILURE
        return None

    if file_path == "SKILL.md" or file_path.startswith("references/") or file_path.lower().endswith((".md", ".markdown")):
        if not _frontmatter_closed(text) or not _balanced_markdown_fences(text):
            return MARKDOWN_BASIC_FAILURE
        return None

    return None
