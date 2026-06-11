"""Document runtime helpers for generated Skill scripts.

The helpers in this module intentionally import optional document libraries
inside each function.  That keeps ``backend.services.skill_runtime`` importable
in lightweight environments while still making Creator tool status aware that
these helpers exist and are platform-owned.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_filename(filename: str, default: str) -> str:
    candidate = _SAFE_FILENAME_RE.sub("-", str(filename or default).strip()).strip("-._")
    return candidate or default


def _output_path(
    *,
    output_path: str | os.PathLike[str] | None,
    output_dir: str | os.PathLike[str] | None,
    filename: str,
) -> Path:
    """Resolve a document output path constrained to OUTPUT_DIR.

    Platform helpers must not write arbitrary host paths before the later
    artifact validator has a chance to reject unsafe stdout declarations.  The
    caller may provide a relative subpath, or an absolute path that is already
    inside OUTPUT_DIR, but traversal/absolute escapes are rejected up front.
    """
    out_dir = Path(output_dir or os.environ.get("OUTPUT_DIR") or "outputs").expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if output_path:
        raw_path = Path(output_path).expanduser()
        candidate = raw_path.resolve() if raw_path.is_absolute() else (out_dir / raw_path).resolve()
        try:
            candidate.relative_to(out_dir)
        except ValueError as exc:
            raise ValueError("output_path must stay under OUTPUT_DIR") from exc
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    return out_dir / _safe_filename(filename, filename)


def _coerce_lines(text: str | Iterable[Any]) -> list[str]:
    if isinstance(text, str):
        lines = text.splitlines()
    else:
        lines = [str(item) for item in text]
    return [line if line else " " for line in lines] or ["Generated document"]


def create_pdf(
    text: str | Iterable[Any],
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pdf",
    title: str | None = None,
    font_name: str = "STSong-Light",
) -> dict[str, Any]:
    """Create a Unicode-capable PDF and return JSON-serializable paths.

    ``STSong-Light`` is a built-in ReportLab CID font, so generated Chinese text
    works without bundling a TTF file.  Generated scripts should print the
    returned dict (or include its paths) as stdout JSON.
    """
    pdf_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    _, height = A4
    c.setTitle(title or pdf_path.stem)
    c.setFont(font_name, 14)
    y = height - 72
    for raw_line in _coerce_lines(text):
        line = str(raw_line)
        while line:
            chunk, line = line[:42], line[42:]
            c.drawString(72, y, chunk)
            y -= 22
            if y < 72:
                c.showPage()
                c.setFont(font_name, 14)
                y = height - 72
    c.save()
    return {"pdf_path": str(pdf_path), "file_paths": [str(pdf_path)]}


def create_docx(
    text: str | Iterable[Any],
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.docx",
    title: str | None = None,
) -> dict[str, Any]:
    """Create a Word document and return JSON-serializable paths."""
    docx_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)

    from docx import Document

    document = Document()
    if title:
        document.add_heading(str(title), level=1)
    for line in _coerce_lines(text):
        document.add_paragraph(str(line))
    document.save(str(docx_path))
    return {"docx_path": str(docx_path), "file_paths": [str(docx_path)]}


def create_pptx(
    slides: str | Iterable[Any],
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pptx",
    title: str = "Generated Presentation",
) -> dict[str, Any]:
    """Create a simple PowerPoint deck and return JSON-serializable paths."""
    pptx_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)

    from pptx import Presentation

    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = str(title or "Generated Presentation")
    title_slide.placeholders[1].text = "Created by Superskills runtime tools"

    for index, line in enumerate(_coerce_lines(slides), start=1):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = f"Slide {index}"
        slide.placeholders[1].text = str(line)
    prs.save(str(pptx_path))
    return {"pptx_path": str(pptx_path), "file_paths": [str(pptx_path)]}


def extract_pdf_text(path: str | os.PathLike[str], *, max_pages: int | None = None) -> dict[str, Any]:
    """Extract text from a PDF with pypdf and return page-level text."""
    from pypdf import PdfReader

    pdf_path = Path(path).expanduser().resolve()
    reader = PdfReader(str(pdf_path))
    pages = []
    for index, page in enumerate(reader.pages):
        if max_pages is not None and index >= max_pages:
            break
        pages.append(page.extract_text() or "")
    return {
        "text": "\n".join(pages).strip(),
        "pages": pages,
        "page_count": len(reader.pages),
        "pdf_path": str(pdf_path),
    }
