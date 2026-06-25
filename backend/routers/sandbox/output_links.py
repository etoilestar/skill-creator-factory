"""输出文件链接重写。"""

import re
from pathlib import Path

logger = __import__("logging").getLogger(__name__)


def build_file_download_url(skill_name: str, rel_path: str) -> str:
    """生成文件下载 URL，根据配置决定使用相对路径还是绝对路径。

    当 settings.public_base_url 非空时，生成包含 host 的绝对 URL；
    否则生成相对路径 /api/skills/{skill_name}/files/{rel_path}。
    """
    from ...config import settings
    base = settings.public_base_url.rstrip("/") if settings.public_base_url else ""
    path = f"/api/skills/{skill_name}/files/{rel_path}"
    return f"{base}{path}" if base else path


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


_SKILL_FILE_PATH_RE = re.compile(r"(?:https?://[^/]+)?/api/skills/[^/]+/files/(.+)")


def _rewrite_output_file_markdown_links(answer: str, output_files: list[dict] | None) -> str:
    """Rewrite relative Markdown links/images for generated files to served URLs."""
    lookup = _output_file_lookup(output_files)
    if not answer or not lookup:
        return answer

    def replace(match: re.Match) -> str:
        prefix, target, suffix = match.groups()
        normalized = _normalize_output_file_ref(target)
        # 先尝试从 lookup 中查找（相对路径匹配）
        url = lookup.get(normalized) or lookup.get(Path(normalized).name)
        if not url:
            # 对绝对链接（LLM 编造的 http://127.0.0.1:8080/api/skills/... 或 /api/skills/...），
            # 提取路径部分再尝试匹配
            if "://" in target or target.startswith("/"):
                path_match = _SKILL_FILE_PATH_RE.match(target)
                if path_match:
                    file_rel = path_match.group(1)
                    url = lookup.get(file_rel) or lookup.get(Path(file_rel).name)
        if url:
            return f"{prefix}{url}{suffix}"
        return match.group(0)

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
