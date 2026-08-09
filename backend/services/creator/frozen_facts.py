"""Canonical ownership and deterministic projections for Creator frozen facts."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

FACT_OWNERSHIP_CONTRACT = """FACT OWNERSHIP CONTRACT

Each semantic fact has exactly one authoritative owner stage. Once frozen,
downstream stages may read it, validate references to it, deterministically
project it, or repair only an explicitly editable downstream layer. They must
not regenerate, reinterpret, silently overwrite, or create a second
authoritative representation of the same fact."""

FROZEN_FACT_AUTHORITY = """FROZEN FACT AUTHORITY

Frozen facts are not proposals. Do not revise, reinterpret, rename, expand, or
replace a frozen fact unless this stage has explicit repair authority over that
fact. When the task only describes or uses a frozen fact, reference it as given."""

CANONICAL_PROJECTION_PRINCIPLE = """CANONICAL PROJECTION PRINCIPLE

If a downstream field can be fully derived from frozen authoritative facts,
derive it deterministically. Do not ask another model to regenerate it. Use a
model only when a genuinely new semantic decision is required."""

FACT_OWNERS: dict[str, str] = {
    "confirmed_requirements": "requirement",
    "file_plan": "blueprint_freeze",
    "function_items": "blueprint_freeze",
    "requirement_projection": "requirement_projection",
    "resource_authority": "resource_authority",
    "platform_contract": "platform_contract",
    "interface_plan": "interface_planner",
    "graph": "graph_backend",
    "tool_bindings": "tool_planner",
    "generated_file_content": "generation",
    "review_summary_prose": "review_summary",
}


@dataclass(frozen=True)
class CreatorFactsSnapshot:
    """Lightweight read-only handoff; fields are owned according to FACT_OWNERS."""

    confirmed_requirements: tuple[Any, ...] = ()
    file_plan: tuple[Any, ...] = ()
    function_items: tuple[Any, ...] = ()
    requirement_projection: dict[str, Any] = field(default_factory=dict)
    resource_authority: dict[str, Any] = field(default_factory=dict)
    platform_contract: dict[str, Any] = field(default_factory=dict)
    interface_plan: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=dict)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def frozen_fact_digests(snapshot: CreatorFactsSnapshot) -> dict[str, str]:
    """Presentation-insensitive stage digests used only for diagnostics."""
    function_items = sorted((
        str(item.get("target_file") or ""),
        " ".join(str(item.get("purpose") or item.get("responsibility") or "").split()),
        tuple(sorted(str(value) for value in (item.get("inputs") or []))),
        tuple(sorted(str(value) for value in (item.get("outputs") or []))),
        tuple(sorted(str(value) for value in (item.get("dependencies") or []))),
    ) for item in snapshot.function_items if isinstance(item, dict))
    projection = snapshot.requirement_projection
    allocations = sorted((
        str(item.get("requirement_id") or ""),
        str((projection.get("channels") or {}).get(str(item.get("requirement_id") or "")) or ""),
        tuple(sorted(str(owner) for owner in (item.get("owners") or []))),
    ) for item in (projection.get("allocations") or []) if isinstance(item, dict))
    resources = snapshot.resource_authority
    return {
        "function_items": _digest(function_items),
        "requirement_projection": _digest(allocations),
        "resource_authority": _digest({
            key: sorted(str(value) for value in (resources.get(key) or []))
            for key in ("authoritative_references", "authoritative_assets", "allowed_resources")
        }),
    }


def log_frozen_fact_digests(*, stage: str, snapshot: CreatorFactsSnapshot) -> None:
    for kind, digest in frozen_fact_digests(snapshot).items():
        logger.info("[Creator][frozen_fact] stage=%s kind=%s digest=%s", stage, kind, digest)


def project_frozen_facts_to_summary(
    *, summary_prose: dict[str, Any], authoritative_files: list[str],
    authoritative_upload_assets: list[str],
) -> dict[str, Any]:
    """Pure identity projection; no path, extension, or business heuristics."""
    allowed_prose = ("goal", "input", "output", "workflow", "risks", "changes")
    projected = {key: summary_prose.get(key, [] if key in {"workflow", "risks", "changes"} else "")
                 for key in allowed_prose}
    projected["files_to_create_or_update"] = list(dict.fromkeys(authoritative_files))
    projected["assets_to_upload"] = list(dict.fromkeys(authoritative_upload_assets))
    return projected
