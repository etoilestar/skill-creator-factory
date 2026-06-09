"""输出文件链接重写。"""

import re
from pathlib import Path

logger = __import__("logging").getLogger(__name__)


_MARKDOWN_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^()\s]+)(\))")


def _is_external_or_absolute_link(target: str) -> bool:
    lowered = target.strip().lower()
    return bool(
        re.match(r"^[a-z][a-z0-9+.-]*:", lowered)
        or lowered.startswith("//")
        or lowered.startswith("/")
        or lowered.startswith("#")
    )


def _normalize_output_file_ref(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _output_file_lookup(output_files: list[dict] | None) -> dict[str, str]:
    """Build path/basename -> download URL lookup for generated files."""
    lookup: dict[str, str] = {}
    for item in output_files or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        path = _normalize_output_file_ref(str(item.get("path") or ""))
        if not url or not path:
            continue
        lookup[path] = url
        lookup[Path(path).name] = url
    return lookup


def _rewrite_output_file_markdown_links(answer: str, output_files: list[dict] | None) -> str:
    """Rewrite relative Markdown links/images for generated files to served URLs."""
    lookup = _output_file_lookup(output_files)
    if not answer or not lookup:
        return answer

    def replace(match: re.Match) -> str:
        prefix, target, suffix = match.groups()
        if _is_external_or_absolute_link(target):
            return match.group(0)
        normalized = _normalize_output_file_ref(target)
        url = lookup.get(normalized) or lookup.get(Path(normalized).name)
        if not url:
            return match.group(0)
        return f"{prefix}{url}{suffix}"

    return _MARKDOWN_LINK_RE.sub(replace, answer)


def _finalize_answer_output_file_links(answer: str, output_files: list[dict] | None) -> str:
    """Rewrite only file links the final answer already chose to show.

    Do not append generated files automatically: many Skills create auxiliary
    artifacts that should stay available through the structured output_files
    event/download bar without being forced into the final chat answer.
    """
    return _rewrite_output_file_markdown_links(answer, output_files)


# Public alias
finalize_answer_output_file_links = _finalize_answer_output_file_links
