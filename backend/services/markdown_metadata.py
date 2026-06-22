"""Deterministic Markdown frontmatter parsing and validation for Creator."""

from __future__ import annotations

from typing import Any
import yaml

_ALLOWED_SKILL_TOP_LEVEL = {"name", "description", "license", "allowed-tools", "metadata"}
_FORBIDDEN_SKILL_TOP_LEVEL = {
    "trigger", "inputs", "outputs", "dependencies", "required_capabilities",
    "business_forbidden_capabilities", "references", "assets", "role", "path",
    "type", "scope", "workflow", "script_order", "resource_references",
}
_ALLOWED_REFERENCE_TOP_LEVEL = {"title", "description", "source", "license", "metadata"}
_FORBIDDEN_REFERENCE_TOP_LEVEL = {
    "trigger", "inputs", "outputs", "dependencies", "required_capabilities",
    "business_forbidden_capabilities", "assets", "references", "role", "path",
    "workflow", "script_order", "file_plan", "capabilities",
}


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any] | None, str, bool]:
    """Return (frontmatter, body, had_frontmatter).

    Missing frontmatter is not an error for generic/reference Markdown. Invalid
    YAML is represented as an empty non-None mapping when delimiters exist.
    """
    text = (markdown or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return None, markdown or "", False
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, markdown or "", False
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, "".join(lines[1:]), True
    raw = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, body, True


def validate_generic_frontmatter(frontmatter: dict[str, Any] | None, *, allowed: set[str], forbidden: set[str], require_name_description: bool = False, allow_missing: bool = True) -> list[str]:
    if frontmatter is None:
        return [] if allow_missing else ["missing frontmatter"]
    keys = set(str(k) for k in frontmatter.keys())
    errors: list[str] = []
    extra = sorted(keys - allowed)
    bad = sorted(keys & forbidden)
    if extra:
        errors.append("forbidden/unknown top-level keys: " + ", ".join(extra))
    if bad:
        errors.append("Creator planning keys are not allowed in frontmatter: " + ", ".join(bad))
    if require_name_description:
        for key in ("name", "description"):
            if not str(frontmatter.get(key) or "").strip():
                errors.append(f"missing required key: {key}")
    return errors


def validate_skill_frontmatter(frontmatter: dict[str, Any] | None) -> list[str]:
    return validate_generic_frontmatter(frontmatter, allowed=_ALLOWED_SKILL_TOP_LEVEL, forbidden=_FORBIDDEN_SKILL_TOP_LEVEL, require_name_description=True, allow_missing=False)


def validate_reference_frontmatter(frontmatter: dict[str, Any] | None) -> list[str]:
    return validate_generic_frontmatter(frontmatter, allowed=_ALLOWED_REFERENCE_TOP_LEVEL, forbidden=_FORBIDDEN_REFERENCE_TOP_LEVEL, allow_missing=True)



def _clean_metadata_creator(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        creator = value.get("creator")
        if isinstance(creator, dict):
            return {str(k): v for k, v in creator.items() if v not in (None, "", [], {})}
    return {}


def canonicalize_skill_frontmatter(frontmatter: dict[str, Any] | None, *, default_name: str = "", default_description: str = "") -> dict[str, Any]:
    """Return SKILL.md frontmatter with only allowed top-level keys.

    Unknown/Creator planning keys are preserved under metadata.creator so the
    Markdown body does not need to be rewritten to remove them.
    """
    source = frontmatter if isinstance(frontmatter, dict) else {}
    canonical: dict[str, Any] = {}
    for key in ("name", "description", "license", "allowed-tools"):
        if source.get(key) not in (None, "", [], {}):
            canonical[key] = source[key]
    canonical.setdefault("name", default_name or "skill")
    canonical.setdefault("description", default_description or canonical.get("name") or "Skill description")

    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    clean_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    creator = _clean_metadata_creator(clean_metadata)
    for key, value in source.items():
        if key in _ALLOWED_SKILL_TOP_LEVEL or value in (None, "", [], {}):
            continue
        creator[str(key)] = value
    if creator:
        clean_metadata["creator"] = creator
    if clean_metadata:
        canonical["metadata"] = clean_metadata
    return canonical


def canonicalize_reference_frontmatter(frontmatter: dict[str, Any] | None, *, file_path: str = "", purpose: str = "") -> dict[str, Any] | None:
    """Return reference frontmatter with only allowed document metadata keys.

    Missing reference frontmatter stays missing. Disallowed path/purpose/planning
    keys are moved under metadata.creator when frontmatter exists.
    """
    if frontmatter is None:
        return None
    source = frontmatter if isinstance(frontmatter, dict) else {}
    canonical: dict[str, Any] = {}
    for key in ("title", "description", "source", "license"):
        if source.get(key) not in (None, "", [], {}):
            canonical[key] = source[key]
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    clean_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    creator = _clean_metadata_creator(clean_metadata)
    if file_path:
        creator.setdefault("path", file_path)
    if purpose:
        creator.setdefault("purpose", purpose)
    for key, value in source.items():
        if key in _ALLOWED_REFERENCE_TOP_LEVEL or value in (None, "", [], {}):
            continue
        creator[str(key)] = value
    if creator:
        clean_metadata["creator"] = creator
    if clean_metadata:
        canonical["metadata"] = clean_metadata
    if not canonical:
        return None
    return canonical

def apply_frontmatter_patch(markdown: str, new_frontmatter: dict[str, Any] | None) -> str:
    """Replace only the YAML frontmatter, preserving body byte-for-byte text."""
    _old, body, had = parse_frontmatter(markdown)
    if new_frontmatter is None:
        return body if had else (markdown or "")
    yaml_text = yaml.safe_dump(new_frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    separator = "" if body.startswith("\n") or not body else "\n"
    return f"---\n{yaml_text}\n---\n{separator}{body}"
