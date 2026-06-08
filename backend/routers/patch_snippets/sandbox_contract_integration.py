"""Patch snippets for backend/routers/sandbox_chat.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.services.skill_contract import WorkflowContract
from backend.services.skill_contract_validator import validate_stdout_against_output_schema


def load_skill_contract_if_exists(execution_root: Path) -> WorkflowContract | None:
    path = execution_root / "contract.json"
    if not path.is_file():
        return None
    try:
        return WorkflowContract.read_json(path)
    except Exception:
        return None


def enrich_resource_catalog_with_reference_metadata(resource: dict[str, Any], execution_root: Path) -> dict[str, Any]:
    """Call this inside resource catalog construction for references/*.md.

    It reads only a small frontmatter block, not the full body.
    """
    path = str(resource.get("path") or "")
    if not path.startswith("references/"):
        return resource

    file_path = (execution_root / path).resolve()
    if not file_path.is_file():
        return resource

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")[:4096]
    except Exception:
        return resource

    meta = _parse_frontmatter_metadata(text)
    resource.update({k: v for k, v in meta.items() if v not in (None, "", [])})
    return resource


def _parse_frontmatter_metadata(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end].strip()
    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [x.strip().strip("'\"") for x in value[1:-1].split(",") if x.strip()]
            meta[key] = items
        else:
            meta[key] = value.strip("'\"")
    return meta


def validate_runtime_step_stdout_with_contract(
    *,
    contract: WorkflowContract | None,
    script_path: str,
    payload: dict[str, Any],
    execution_root: Path,
) -> list[dict[str, Any]]:
    if contract is None:
        return []
    step = contract.step_by_script_path(script_path)
    if step is None:
        return []
    issues = validate_stdout_against_output_schema(payload, step, execution_root=execution_root)
    return [x.to_dict() for x in issues]
