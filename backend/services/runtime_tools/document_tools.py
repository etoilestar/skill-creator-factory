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



def create_text_file(
    text: str,
    filename: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create a UTF-8 TXT file and return JSON-serializable artifact paths."""
    safe_name = _safe_filename(filename or "output.txt", "output.txt")
    if not safe_name.lower().endswith(".txt"):
        safe_name = f"{safe_name}.txt"
    text_path = _output_path(output_path=None, output_dir=output_dir, filename=safe_name)
    text_path.write_text(str(text or ""), encoding="utf-8")
    return {"text_path": str(text_path), "file_paths": [str(text_path)], "file_outputs": [str(text_path)]}

def _artifact_result(path: Path, *, artifact_type: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        f"{artifact_type}_path": str(path),
        "file_paths": [str(path)],
        "file_outputs": [str(path)],
    }
    if metadata:
        result["artifact_metadata"] = {
            "artifact_type": artifact_type,
            **metadata,
        }
    return result

def _write_minimal_trial_pdf(path: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF\n"
    )
    return _artifact_result(path, artifact_type="pdf", metadata=metadata)


def _normalize_pdf_styles(styles: dict[str, Any] | None) -> dict[str, Any]:
    styles = dict(styles or {})
    return {
        "page_size": str(styles.get("page_size") or "A4"),
        "font_name": str(styles.get("font_name") or styles.get("font_family") or "STSong-Light"),
        "font_path": str(styles.get("font_path") or "").strip(),
        "title_font_size": float(styles.get("title_font_size") or 20),
        "heading_font_size": float(styles.get("heading_font_size") or 16),
        "body_font_size": float(styles.get("body_font_size") or styles.get("font_size") or 12),
        "caption_font_size": float(styles.get("caption_font_size") or 9),
        "line_spacing": float(styles.get("line_spacing") or 1.35),
        "first_line_indent": float(styles.get("first_line_indent") or 0),
        "space_before": float(styles.get("space_before") or 0),
        "space_after": float(styles.get("space_after") or 8),
        "margin_left": float(styles.get("margin_left") or styles.get("margin") or 72),
        "margin_right": float(styles.get("margin_right") or styles.get("margin") or 72),
        "margin_top": float(styles.get("margin_top") or styles.get("margin") or 72),
        "margin_bottom": float(styles.get("margin_bottom") or styles.get("margin") or 72),
        "image_max_width": float(styles.get("image_max_width") or 420),
        "image_max_height": float(styles.get("image_max_height") or 360),
    }


def _normalize_document_blocks(blocks: Any) -> list[dict[str, Any]]:
    if isinstance(blocks, str):
        return [{"type": "paragraph", "text": line} for line in _coerce_lines(blocks)]

    if isinstance(blocks, dict):
        if isinstance(blocks.get("blocks"), list):
            blocks = blocks["blocks"]
        else:
            return [dict(blocks)]

    if not isinstance(blocks, list):
        try:
            blocks = list(blocks)
        except Exception:
            blocks = [{"type": "paragraph", "text": str(blocks or "Generated document")}]

    normalized: list[dict[str, Any]] = []
    for item in blocks:
        if isinstance(item, dict):
            block = dict(item)
        else:
            block = {"type": "paragraph", "text": str(item)}
        block_type = str(block.get("type") or "paragraph").strip().lower()
        block["type"] = block_type
        normalized.append(block)

    return normalized or [{"type": "paragraph", "text": "Generated document"}]


def _register_reportlab_font(font_name: str, font_path: str = "") -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    # Built-in CID fonts work well for Chinese without bundling font files.
    if not font_path:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        except Exception:
            if font_name != "STSong-Light":
                font_name = "STSong-Light"
                pdfmetrics.registerFont(UnicodeCIDFont(font_name))
            else:
                raise
        return font_name

    from reportlab.pdfbase.ttfonts import TTFont

    safe_font_path = _safe_input_path(font_path, {".ttf", ".otf"})
    pdfmetrics.registerFont(TTFont(font_name, str(safe_font_path)))
    return font_name


def create_pdf_document(
    blocks: list[dict[str, Any]] | dict[str, Any] | str | Iterable[Any],
    *,
    styles: dict[str, Any] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pdf",
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a structured PDF document from blocks and return artifact paths.

    Supported block types:
    - title: {"type": "title", "text": "..."}
    - heading: {"type": "heading", "level": 1, "text": "..."}
    - paragraph/text: {"type": "paragraph", "text": "..."}
    - image: {"type": "image", "path": "...", "caption": "..."}
    - table: {"type": "table", "headers": [...], "rows": [[...], ...]}
    - spacer: {"type": "spacer", "height": 12}
    - page_break: {"type": "page_break"}

    Generated Skill scripts should print the returned dict, or include its
    pdf_path/file_outputs fields in stdout JSON.
    """
    pdf_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)
    style_cfg = _normalize_pdf_styles(styles)
    normalized_blocks = _normalize_document_blocks(blocks)

    result_metadata = {
        "creator_tool": "create_pdf_document",
        "block_count": len(normalized_blocks),
        "styles": {
            key: value
            for key, value in style_cfg.items()
            if key != "font_path"
        },
        **(metadata or {}),
    }

    if _trial():
        return _write_minimal_trial_pdf(pdf_path, metadata=result_metadata)

    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        Image as RLImage,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    page_size_name = str(style_cfg["page_size"]).upper()
    page_size = LETTER if page_size_name in {"LETTER", "US_LETTER"} else A4
    font_name = _register_reportlab_font(str(style_cfg["font_name"]), str(style_cfg.get("font_path") or ""))

    base_styles = getSampleStyleSheet()
    body_font_size = float(style_cfg["body_font_size"])
    body_leading = body_font_size * float(style_cfg["line_spacing"])

    title_style = ParagraphStyle(
        "SuperskillsTitle",
        parent=base_styles["Title"],
        fontName=font_name,
        fontSize=float(style_cfg["title_font_size"]),
        leading=float(style_cfg["title_font_size"]) * 1.25,
        spaceAfter=14,
    )
    heading_style = ParagraphStyle(
        "SuperskillsHeading",
        parent=base_styles["Heading1"],
        fontName=font_name,
        fontSize=float(style_cfg["heading_font_size"]),
        leading=float(style_cfg["heading_font_size"]) * 1.25,
        spaceBefore=10,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "SuperskillsBody",
        parent=base_styles["BodyText"],
        fontName=font_name,
        fontSize=body_font_size,
        leading=body_leading,
        firstLineIndent=float(style_cfg["first_line_indent"]),
        spaceBefore=float(style_cfg["space_before"]),
        spaceAfter=float(style_cfg["space_after"]),
    )
    caption_style = ParagraphStyle(
        "SuperskillsCaption",
        parent=base_styles["BodyText"],
        fontName=font_name,
        fontSize=float(style_cfg["caption_font_size"]),
        leading=float(style_cfg["caption_font_size"]) * 1.25,
        spaceBefore=4,
        spaceAfter=8,
    )

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=page_size,
        leftMargin=float(style_cfg["margin_left"]),
        rightMargin=float(style_cfg["margin_right"]),
        topMargin=float(style_cfg["margin_top"]),
        bottomMargin=float(style_cfg["margin_bottom"]),
        title=title or pdf_path.stem,
    )

    story = []

    def paragraph_text(value: Any) -> str:
        return escape(str(value or "")).replace("\n", "<br/>")

    for block in normalized_blocks:
        block_type = str(block.get("type") or "paragraph").lower()

        if block_type == "title":
            story.append(Paragraph(paragraph_text(block.get("text")), title_style))
            continue

        if block_type == "heading":
            story.append(Paragraph(paragraph_text(block.get("text")), heading_style))
            continue

        if block_type in {"paragraph", "text"}:
            text = str(block.get("text") or block.get("content") or "")
            if not text.strip():
                story.append(Spacer(1, float(style_cfg["space_after"])))
            else:
                for part in text.split("\n\n"):
                    story.append(Paragraph(paragraph_text(part), body_style))
            continue

        if block_type == "list":
            items = block.get("items") or []
            if isinstance(items, list):
                flow_items = [
                    ListItem(Paragraph(paragraph_text(item), body_style))
                    for item in items
                ]
                story.append(ListFlowable(flow_items, bulletType="bullet"))
            continue

        if block_type == "image":
            raw_path = str(block.get("path") or block.get("image_path") or "").strip()
            if not raw_path:
                continue
            image_path = _safe_input_path(raw_path, {".png", ".jpg", ".jpeg", ".webp"})
            image = RLImage(str(image_path))
            max_w = float(block.get("max_width") or style_cfg["image_max_width"])
            max_h = float(block.get("max_height") or style_cfg["image_max_height"])
            scale = min(max_w / image.drawWidth, max_h / image.drawHeight, 1.0)
            image.drawWidth *= scale
            image.drawHeight *= scale
            story.append(image)
            if block.get("caption"):
                story.append(Paragraph(paragraph_text(block.get("caption")), caption_style))
            continue

        if block_type == "table":
            headers = block.get("headers") or []
            rows = block.get("rows") or []
            table_data = []
            if headers:
                table_data.append(headers)
            table_data.extend(rows if isinstance(rows, list) else [])
            if table_data:
                rendered_rows = [
                    [Paragraph(paragraph_text(cell), body_style) for cell in row]
                    for row in table_data
                ]
                table = Table(rendered_rows, repeatRows=1 if headers else 0)
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 6),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(table)
                story.append(Spacer(1, 8))
            continue

        if block_type in {"page_break", "pagebreak", "break"}:
            story.append(PageBreak())
            continue

        if block_type == "spacer":
            story.append(Spacer(1, float(block.get("height") or 12)))
            continue

        story.append(Paragraph(paragraph_text(block.get("text") or block), body_style))

    if not story:
        story.append(Paragraph("Generated document", body_style))

    doc.build(story)
    return _artifact_result(pdf_path, artifact_type="pdf", metadata=result_metadata)

def create_pdf(
    text: str | Iterable[Any],
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pdf",
    title: str | None = None,
    font_name: str = "STSong-Light",
    font_size: float = 14,
    line_spacing: float = 1.35,
) -> dict[str, Any]:
    """Create a simple Unicode-capable PDF from plain text.

    This is the compatibility wrapper for simple text-to-PDF tasks.  For
    structured documents with headings, images, tables, or layout requirements,
    generated Skill scripts should use create_pdf_document instead.
    """
    blocks: list[dict[str, Any]] = []
    if title:
        blocks.append({"type": "title", "text": title})
    blocks.extend({"type": "paragraph", "text": line} for line in _coerce_lines(text))

    return create_pdf_document(
        blocks,
        output_path=output_path,
        output_dir=output_dir,
        filename=filename,
        title=title,
        styles={
            "font_name": font_name,
            "body_font_size": font_size,
            "line_spacing": line_spacing,
        },
        metadata={
            "creator_tool": "create_pdf",
            "compatibility_wrapper": True,
        },
    )


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
    return {"docx_path": str(docx_path), "file_paths": [str(docx_path)], "file_outputs": [str(docx_path)]}


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
    return {"pptx_path": str(pptx_path), "file_paths": [str(pptx_path)], "file_outputs": [str(pptx_path)]}


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

_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_INPUT_FILES = 50


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _allowed_input_roots() -> list[Path]:
    roots = [Path.cwd()]
    for name in ("SKILL_WORKDIR", "SKILL_DIR", "INPUT_DIR", "UPLOAD_DIR", "OUTPUT_DIR"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]))
    roots.extend([Path.cwd() / "inputs", Path.cwd() / "assets", Path.cwd() / "uploads"])
    return [root.expanduser().resolve() for root in roots]


def _safe_input_path(path: str | os.PathLike[str], suffixes: set[str]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.suffix.lower() not in suffixes:
        raise ValueError(f"unsupported file type: {resolved.suffix}")
    if not resolved.is_file():
        raise FileNotFoundError("input file does not exist")
    if resolved.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("input file is too large")
    if not any(resolved == root or resolved.is_relative_to(root) for root in _allowed_input_roots()):
        raise ValueError("input path must stay under the skill workdir, inputs, assets, uploads, or OUTPUT_DIR")
    return resolved


def read_docx_text(docx_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read text from a Word document."""
    if _trial():
        return {"text": "Mock DOCX text during SKILL_TRIAL_RUN.", "paragraphs": ["Mock DOCX text during SKILL_TRIAL_RUN."], "source_path": str(docx_path)}
    path = _safe_input_path(docx_path, {".docx"})
    from docx import Document

    document = Document(str(path))
    paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    return {"text": "\n".join(paragraphs), "paragraphs": paragraphs, "source_path": str(path)}


def read_pptx_text(pptx_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read text from a PowerPoint deck."""
    if _trial():
        return {"text": "Mock PPTX text during SKILL_TRIAL_RUN.", "paragraphs": ["Mock PPTX text during SKILL_TRIAL_RUN."], "source_path": str(pptx_path)}
    path = _safe_input_path(pptx_path, {".pptx"})
    from pptx import Presentation

    paragraphs: list[str] = []
    for slide in Presentation(str(path)).slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                paragraphs.append(str(shape.text))
    return {"text": "\n".join(paragraphs), "paragraphs": paragraphs, "source_path": str(path)}


def read_spreadsheet(path: str | os.PathLike[str], sheet_name: str | None = None, max_rows: int = 500) -> dict[str, Any]:
    """Read rows from an Excel spreadsheet."""
    max_rows = max(1, min(int(max_rows or 500), 5000))
    if _trial():
        return {"sheets": [sheet_name or "Sheet1"], "columns": ["A", "B"], "rows": [{"A": "mock", "B": "value"}], "row_count": 1, "truncated": False}
    safe_path = _safe_input_path(path, {".xlsx", ".xlsm"})
    from openpyxl import load_workbook

    workbook = load_workbook(str(safe_path), read_only=True, data_only=True)
    worksheet = workbook[sheet_name] if sheet_name else workbook[workbook.sheetnames[0]]
    rows_iter = worksheet.iter_rows(values_only=True)
    header_values = next(rows_iter, None) or []
    columns = [str(value) if value not in (None, "") else f"Column{index}" for index, value in enumerate(header_values, start=1)]
    rows: list[dict[str, Any]] = []
    truncated = False
    for index, values in enumerate(rows_iter, start=1):
        if index > max_rows:
            truncated = True
            break
        rows.append({columns[i] if i < len(columns) else f"Column{i+1}": value for i, value in enumerate(values)})
    return {"sheets": workbook.sheetnames, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated, "source_path": str(safe_path)}


def merge_pdfs(pdf_paths: list[str], output_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Merge PDFs into a PDF under OUTPUT_DIR."""
    if not pdf_paths or len(pdf_paths) > _MAX_INPUT_FILES:
        raise ValueError("pdf_paths must contain 1 to 50 files")
    out_path = _output_path(output_path=output_path, output_dir=None, filename="merged.pdf")
    if _trial():
        return _write_minimal_trial_pdf(out_path)
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for raw_path in pdf_paths:
        reader = PdfReader(str(_safe_input_path(raw_path, {".pdf"})))
        for page in reader.pages:
            writer.add_page(page)
    with out_path.open("wb") as file_obj:
        writer.write(file_obj)
    return {"pdf_path": str(out_path), "file_paths": [str(out_path)], "file_outputs": [str(out_path)]}


def images_to_pdf(image_paths: list[str], output_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Convert images to one PDF under OUTPUT_DIR."""
    if not image_paths or len(image_paths) > _MAX_INPUT_FILES:
        raise ValueError("image_paths must contain 1 to 50 files")
    out_path = _output_path(output_path=output_path, output_dir=None, filename="images.pdf")
    if _trial():
        return _write_minimal_trial_pdf(out_path)
    from PIL import Image

    images = []
    for raw_path in image_paths:
        image = Image.open(_safe_input_path(raw_path, {".png", ".jpg", ".jpeg", ".webp"})).convert("RGB")
        images.append(image)
    first, rest = images[0], images[1:]
    first.save(str(out_path), save_all=True, append_images=rest)
    return {"pdf_path": str(out_path), "file_paths": [str(out_path)], "file_outputs": [str(out_path)]}


def build_pdf_report(
    title: str,
    sections: list[dict],
    image_paths: list[str] | None = None,
    *,
    filename: str = "report.pdf",
    styles: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured PDF report from sections and optional images."""
    blocks: list[dict[str, Any]] = [{"type": "title", "text": str(title or "Report")}]

    for section in sections or []:
        section_title = str(section.get("title") or "Section")
        section_text = str(section.get("text") or section.get("content") or "")
        blocks.append({"type": "heading", "text": section_title})
        blocks.append({"type": "paragraph", "text": section_text})

        section_images = section.get("image_paths") or section.get("images") or []
        if isinstance(section_images, str):
            section_images = [section_images]
        if isinstance(section_images, list):
            for image_path in section_images[:_MAX_INPUT_FILES]:
                blocks.append(
                    {
                        "type": "image",
                        "path": str(image_path),
                        "caption": section.get("image_caption") or "",
                    }
                )

    for image_path in (image_paths or [])[:_MAX_INPUT_FILES]:
        blocks.append({"type": "image", "path": str(image_path)})

    return create_pdf_document(
        blocks,
        styles=styles,
        filename=filename or "report.pdf",
        title=title or "Report",
        metadata={
            "creator_tool": "build_pdf_report",
            "section_count": len(sections or []),
            "image_count": len(image_paths or []),
        },
    )
