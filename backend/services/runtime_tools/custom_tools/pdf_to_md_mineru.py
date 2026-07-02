"""Custom Creator tool adapter for MinerU-style PDF to Markdown conversion."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _output_dir() -> Path:
    out = Path(os.environ.get("OUTPUT_DIR") or os.environ.get("TOOL_OUTPUT_DIR") or "outputs").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _mock_result(pdf_path: str) -> dict[str, Any]:
    output = _output_dir() / "mineru_output.md"
    text = f"# Trial PDF Markdown\n\nDeterministic trial conversion for `{Path(pdf_path).name or 'input.pdf'}`.\n"
    output.write_text(text, encoding="utf-8")
    return {
        "success": True,
        "markdown_path": str(output),
        "markdown_text": text,
        "text": text,
        "file_outputs": [str(output)],
        "artifact_metadata": {"trial_run": True, "tool": "pdf_to_md_mineru"},
    }


def pdf_to_md_mineru(payload: dict | None = None, config: dict | None = None) -> dict[str, Any]:
    """Convert a PDF into Markdown text/path using a configured MinerU endpoint.

    During SKILL_TRIAL_RUN this returns a deterministic local Markdown artifact.
    Real execution requires MINERU_PDF_TO_MD_URL or config['api_url'].
    """
    payload = dict(payload or {})
    config = dict(config or {})
    pdf_path = str(payload.get("pdf_path") or payload.get("input_path") or payload.get("path") or "").strip()
    if os.environ.get("SKILL_TRIAL_RUN") == "1" or os.environ.get("TOOL_TRIAL_RUN") == "1":
        return _mock_result(pdf_path or "input.pdf")
    if not pdf_path:
        raise ValueError("pdf_to_md_mineru requires payload.pdf_path or payload.input_path")
    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError(f"pdf_to_md_mineru requires an existing .pdf file: {pdf_path}")
    api_url = str(config.get("api_url") or os.environ.get("MINERU_PDF_TO_MD_URL") or "").strip()
    if not api_url:
        raise RuntimeError("MINERU_PDF_TO_MD_URL is required for real pdf_to_md_mineru execution")
    import requests
    with source.open("rb") as file_obj:
        response = requests.post(api_url, files={"file": file_obj}, data={"task_type": "skill"}, timeout=120)
    response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        data = {"markdown_text": response.text}
    text = str(data.get("markdown_text") or data.get("markdown") or data.get("content") or data.get("text") or "")
    output = _output_dir() / f"{source.stem}.md"
    output.write_text(text, encoding="utf-8")
    return {
        "success": True,
        "markdown_path": str(output),
        "markdown_text": text,
        "text": text,
        "file_outputs": [str(output)],
        "artifact_metadata": {"tool": "pdf_to_md_mineru", "source_path": str(source)},
    }


def run(payload: dict | None = None, config: dict | None = None) -> dict[str, Any]:
    return pdf_to_md_mineru(payload, config)
