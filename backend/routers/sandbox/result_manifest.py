"""Presentation manifest built only from executor-confirmed observations."""

import json
import mimetypes
from pathlib import Path


def build_result_manifest(execution_result: dict, execution_root: Path | None = None) -> dict:
    structured = []
    for result in execution_result.get("results") or [execution_result]:
        value = result.get("stdout") if isinstance(result, dict) else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                value = None
        if isinstance(value, dict):
            structured.append({"source": "stdout", "data": value})

    artifacts = []
    for item in execution_result.get("output_files") or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "")
        actual = (execution_root / rel).resolve() if execution_root and rel else None
        # output_files is the executor's truth; filesystem metadata only enriches it.
        name = str(item.get("name") or Path(rel).name)
        artifacts.append({
            "path": rel, "url": item.get("url"), "name": name,
            "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": actual.stat().st_size if actual and actual.is_file() else int(item.get("size") or 0),
        })
    return {"version": "sandbox-result-v1", "structured_outputs": structured, "artifacts": artifacts}
