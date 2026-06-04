"""SkillPlan role/contracts for Creator file generation.

This module provides a small, deterministic planning layer between a parsed
blueprint and file generation.  It intentionally keeps keyword signals as
classification hints only; downstream generation/validation is driven by the
resolved file role rather than by scanning the whole blueprint for domain words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

FileType = Literal["skill", "script", "reference", "asset"]
ScriptRole = Literal["text_generator", "image_generator", "pdf_builder", "generic_script"]
ResourceRole = Literal["skill_overview", "reference", "asset"]
FileRole = ScriptRole | ResourceRole

SCRIPT_ROLES: frozenset[str] = frozenset({
    "text_generator",
    "image_generator",
    "pdf_builder",
    "generic_script",
})
RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset"})


@dataclass(frozen=True)
class RoleClassification:
    """Classifier output for a single file role decision."""

    role: FileRole
    confidence: float
    reason: str
    heuristic_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillPlanEntry:
    """Contract for one file that Creator will generate."""

    path: str
    file_type: FileType
    role: FileRole
    purpose: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)
    required: bool = True
    can_skip: bool = False
    confidence: float = 0.0
    reason: str = ""
    heuristic_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillPlan:
    """Full Creator plan for a skill package."""

    skill_name: str
    files: list[SkillPlanEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_IMAGE_RE = re.compile(r"图片|图像|绘图|海报|插画|image|photo|poster|illustration|stable\s*diffusion", re.I)
_PDF_RE = re.compile(r"pdf|报告|排版|layout|document|report", re.I)
_TEXT_RE = re.compile(r"文本|文案|故事|谜语|摘要|写作|text|story|riddle|summary|copy", re.I)
_MODEL_RE = re.compile(r"模型|llm|大语言|多模态|vision|image_model|text_model", re.I)


def file_type_for_path(path: str) -> FileType:
    if path == "SKILL.md":
        return "skill"
    if path.startswith("scripts/"):
        return "script"
    if path.startswith("references/"):
        return "reference"
    return "asset"


def heuristic_signals_for_file(file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[str]:
    """Return role-classification hints without making the final contract decision."""
    text = f"{file_path}\n{purpose}\n{blueprint_summary}"
    signals: list[str] = []
    if _IMAGE_RE.search(text):
        signals.append("mentions_image")
    if _PDF_RE.search(text):
        signals.append("mentions_pdf")
    if _TEXT_RE.search(text):
        signals.append("mentions_text")
    if _MODEL_RE.search(text):
        signals.append("mentions_model")
    if Path(file_path).suffix.lower() == ".py":
        signals.append("python_script")
    return signals



_EXPLICIT_ROLE_RE = re.compile(
    r"(?:role|角色|职责)\s*[：:=]\s*(text_generator|image_generator|pdf_builder|generic_script)",
    re.I,
)


def _normalize_role(value: str) -> FileRole | None:
    lowered = (value or "").strip().lower()
    return lowered if lowered in SCRIPT_ROLES or lowered in RESOURCE_ROLES else None  # type: ignore[return-value]


def _explicit_role_from_plan_text(*, file_path: str, purpose: str = "", blueprint_summary: str = "") -> FileRole | None:
    """Extract an explicit role declared by the plan/model, not by domain keywords."""
    next_path_re = r"(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md"
    for text in (purpose, blueprint_summary):
        for line in (text or "").splitlines():
            if file_path not in line and line.strip() != purpose.strip():
                continue
            segment = line
            if file_path in line:
                segment = re.split(next_path_re, line.split(file_path, 1)[1], maxsplit=1)[0]
            match = _EXPLICIT_ROLE_RE.search(segment)
            if match:
                return _normalize_role(match.group(1))

    match = _EXPLICIT_ROLE_RE.search(purpose or "")
    if match:
        return _normalize_role(match.group(1))
    return None


def file_role_classifier(
    *,
    file_path: str,
    purpose: str = "",
    blueprint_summary: str = "",
    heuristic_signals: list[str] | None = None,
) -> RoleClassification:
    """Classify one file into a Creator role from the plan/model contract.

    Domain keyword/regex matches are collected as heuristic_signals only.  They
    do not directly select skeletons, contracts, capabilities, or specialized
    roles.  Specialized script roles require an explicit plan/model role such
    as ``role: image_generator``.  Ambiguous scripts fall back to conservative
    ``generic_script`` so high-impact capabilities are not enabled by accident.
    """
    file_type = file_type_for_path(file_path)
    signals = list(heuristic_signals or heuristic_signals_for_file(file_path, purpose, blueprint_summary))

    if file_type == "skill":
        return RoleClassification("skill_overview", 1.0, "SKILL.md is the process overview file", signals)
    if file_type == "reference":
        return RoleClassification("reference", 1.0, "references/ files contain subtask guidance", signals)
    if file_type == "asset":
        return RoleClassification("asset", 1.0, "assets/ files are static resources or templates", signals)

    explicit_role = _explicit_role_from_plan_text(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    if explicit_role in SCRIPT_ROLES:
        return RoleClassification(explicit_role, 0.95, "explicit role declared by SkillPlan/blueprint", signals)

    return RoleClassification(
        "generic_script",
        0.45,
        "no explicit script role in SkillPlan; using conservative generic_script fallback",
        signals,
    )


def default_io_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    if role == "text_generator":
        return ["topic", "prompt", "text"], ["text"]
    if role == "image_generator":
        return ["topic", "prompt", "text"], ["image_paths", "images"]
    if role == "pdf_builder":
        return ["text", "image_paths", "template_path"], ["pdf_path", "file_paths"]
    if role == "reference":
        return [], ["non_empty_markdown", "required_sections"]
    if role == "asset":
        return [], ["existing_parseable_file"]
    if role == "skill_overview":
        return ["user_request"], ["workflow", "script_order", "resource_references"]
    return ["payload"], ["text", "file_paths"]


def capabilities_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    if role == "text_generator":
        return ["text_generation"], ["image_generation", "pdf_generation"]
    if role == "image_generator":
        return ["image_generation"], ["pdf_generation"]
    if role == "pdf_builder":
        return ["pdf_generation", "file_output"], ["image_generation"]
    if role == "reference":
        return ["reference_guidance"], ["runtime_execution", "image_generation"]
    if role == "asset":
        return ["static_resource"], ["runtime_execution", "image_generation"]
    if role == "skill_overview":
        return ["workflow_overview"], ["hidden_runtime_protocol"]
    return ["deterministic_execution"], []


def build_skill_plan_entry(
    *,
    file_path: str,
    purpose: str = "",
    required: bool = True,
    can_skip: bool = False,
    blueprint_summary: str = "",
    reference_files: list[str] | None = None,
) -> SkillPlanEntry:
    classification = file_role_classifier(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    inputs, outputs = default_io_for_role(classification.role)
    required_capabilities, forbidden_capabilities = capabilities_for_role(classification.role)
    return SkillPlanEntry(
        path=file_path,
        file_type=file_type_for_path(file_path),
        role=classification.role,
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        dependencies=list(reference_files or []),
        required_capabilities=required_capabilities,
        forbidden_capabilities=forbidden_capabilities,
        reference_files=list(reference_files or []),
        required=required,
        can_skip=can_skip,
        confidence=classification.confidence,
        reason=classification.reason,
        heuristic_signals=classification.heuristic_signals,
    )


def validate_role(role: str, file_type: FileType) -> bool:
    if file_type == "script":
        return role in SCRIPT_ROLES
    return role in RESOURCE_ROLES
