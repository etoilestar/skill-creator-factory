"""SKILL.md 按标题分块与领域标签推断。

将 SKILL.md 全文按 Markdown 标题层级拆分为独立段落，
并基于关键词启发式为每个段落推断 SubAgentType 领域标签。
SubAgent 执行时仅加载匹配自身类型的段落 + 通用段落，
从而大幅缩减注入 LLM 的上下文长度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sub_agent_types import SubAgentType


@dataclass
class SkillSection:
    """SKILL.md 的一个分段。"""

    title: str
    content: str
    level: int  # Markdown 标题层级 (1=#, 2=##, 3=###, ...)
    domain_tags: list[str] = field(default_factory=list)  # SubAgentType.value 列表


# ---------------------------------------------------------------------------
#  领域关键词映射
# ---------------------------------------------------------------------------

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    SubAgentType.DATA_QUERY.value: [
        "数据库", "SQL", "查询", "MySQL", "PostgreSQL", "SQLite",
        "数据统计", "数据导出", "database", "query", "select",
        "insert", "db", "sql", "数据表", "表结构",
    ],
    SubAgentType.CODE_SCRIPT.value: [
        "脚本", "Python", "Shell", "执行", "命令", "运行",
        "script", "python", "bash", "subprocess", "command",
        "pip", "npm", "install", "运行环境", "依赖",
    ],
    SubAgentType.NETWORK_API.value: [
        "HTTP", "API", "请求", "接口", "爬虫", "网络",
        "curl", "http", "api", "request", "fetch", "webhook",
        "URL", "REST", "GET", "POST",
    ],
    SubAgentType.DOCUMENT.value: [
        "Excel", "PDF", "Word", "PPT", "文档", "解析", "导出",
        "格式", "excel", "pdf", "docx", "pptx", "html",
        "markdown", "xlsx", "spreadsheet", "演示", "幻灯片",
    ],
    SubAgentType.VALIDATION.value: [
        "校验", "验证", "审查", "安全", "检查", "validate",
        "verify", "check", "security", "合规", "审计",
    ],
}

# 标题中关键词的权重更高（标题通常更短、更精确）
_TITLE_KEYWORD_BOOST = 2


# ---------------------------------------------------------------------------
#  分段与推断
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def split_skill_md_by_sections(skill_md_text: str) -> list[SkillSection]:
    """将 SKILL.md 按 Markdown 标题分块。

    返回的列表中，第一个元素是 frontmatter + 第一个标题之前的正文（通用块），
    后续元素按标题层级拆分。

    Args:
        skill_md_text: SKILL.md 的完整文本。

    Returns:
        按标题拆分的段落列表，每个段落带有领域标签。
    """
    if not skill_md_text or not skill_md_text.strip():
        return []

    # 找到所有标题位置
    headings: list[tuple[int, int, str, int]] = []  # (start, end, title, level)
    for m in _HEADING_RE.finditer(skill_md_text):
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append((m.start(), m.end(), title, level))

    if not headings:
        # 没有标题，整篇作为通用块
        tags = infer_domain_tags(skill_md_text)
        return [SkillSection(title="", content=skill_md_text, level=0, domain_tags=tags)]

    sections: list[SkillSection] = []

    # 第一个标题之前的内容（frontmatter + 概述）
    first_heading_start = headings[0][0]
    if first_heading_start > 0:
        preamble = skill_md_text[:first_heading_start].strip()
        if preamble:
            # frontmatter/概述通常是通用块，不打领域标签
            sections.append(
                SkillSection(title="", content=preamble, level=0, domain_tags=[])
            )

    # 按标题拆分
    for i, (start, end, title, level) in enumerate(headings):
        # 当前标题到下一个标题之间的内容
        if i + 1 < len(headings):
            section_content = skill_md_text[start : headings[i + 1][0]].strip()
        else:
            section_content = skill_md_text[start:].strip()

        # 推断领域标签（基于标题 + 内容）
        tags = infer_domain_tags(section_content, title_hint=title)
        sections.append(
            SkillSection(title=title, content=section_content, level=level, domain_tags=tags)
        )

    return sections


def infer_domain_tags(section_content: str, *, title_hint: str = "") -> list[str]:
    """基于关键词启发式推断段落的领域标签。

    Args:
        section_content: 段落全文。
        title_hint: 标题文本（可选，标题关键词权重更高）。

    Returns:
        匹配的 SubAgentType.value 列表。空列表表示通用块。
    """
    if not section_content and not title_hint:
        return []

    scores: dict[str, float] = {}
    content_lower = section_content.lower()
    title_lower = title_hint.lower() if title_hint else ""

    for domain, keywords in _DOMAIN_KEYWORDS.items():
        score = 0.0
        for kw in keywords:
            kw_lower = kw.lower()
            # 内容中匹配
            count = content_lower.count(kw_lower)
            score += count
            # 标题中匹配（权重更高）
            if title_lower and kw_lower in title_lower:
                score += _TITLE_KEYWORD_BOOST
        if score > 0:
            scores[domain] = score

    if not scores:
        return []

    # 返回所有有匹配的领域（按分数降序）
    sorted_domains = sorted(scores.keys(), key=lambda d: scores[d], reverse=True)
    return sorted_domains


def filter_sections_for_sub_agent(
    sections: list[SkillSection],
    sub_agent_type: SubAgentType,
) -> str:
    """过滤出 SubAgent 需要的 SKILL.md 段落，重组为精简 prompt。

    规则：
    - 通用块（domain_tags 为空）始终包含
    - 匹配当前 SubAgent 类型的块包含
    - 其他领域的块不包含

    Args:
        sections: split_skill_md_by_sections() 的输出。
        sub_agent_type: 当前 SubAgent 的类型。

    Returns:
        重组后的精简 SKILL.md prompt 文本。
    """
    filtered: list[SkillSection] = []
    for section in sections:
        # 通用块始终包含
        if not section.domain_tags:
            filtered.append(section)
        # 匹配当前 SubAgent 类型的块
        elif sub_agent_type.value in section.domain_tags:
            filtered.append(section)

    if not filtered:
        # 如果没有匹配的段落，回退到第一个通用块（避免空上下文）
        if sections:
            return sections[0].content
        return ""

    return "\n\n".join(s.content for s in filtered)


def get_section_summary(sections: list[SkillSection]) -> list[dict]:
    """生成段落摘要列表，供 Master Agent 了解 SKILL.md 结构。

    Returns:
        [{"title": str, "level": int, "domain_tags": list[str], "chars": int}]
    """
    return [
        {
            "title": s.title or "(通用/前言)",
            "level": s.level,
            "domain_tags": s.domain_tags,
            "chars": len(s.content),
        }
        for s in sections
    ]
