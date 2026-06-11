import json
from zipfile import ZipFile

import pytest

from backend.services.artifact_validator import validate_stdout_file_outputs
from backend.services.runtime_tools.document_tools import create_docx, create_pdf, create_pptx, extract_pdf_text


def test_document_helpers_create_valid_artifacts_and_extract_pdf_text(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("docx")
    pytest.importorskip("pptx")
    pytest.importorskip("pypdf")

    output_dir = tmp_path / "doc-outputs"

    pdf_result = create_pdf("中文测试\n第二行", output_dir=output_dir, filename="report.pdf")
    docx_result = create_docx("中文测试", output_dir=output_dir, filename="report.docx")
    pptx_result = create_pptx(["第一页", "第二页"], output_dir=output_dir, filename="report.pptx")

    pdf_path = output_dir / "report.pdf"
    docx_path = output_dir / "report.docx"
    pptx_path = output_dir / "report.pptx"

    assert pdf_result["pdf_path"] == str(pdf_path)
    assert pdf_result["file_paths"] == [str(pdf_path)]
    assert pdf_result["file_outputs"] == [str(pdf_path)]
    assert docx_result["docx_path"] == str(docx_path)
    assert docx_result["file_paths"] == [str(docx_path)]
    assert docx_result["file_outputs"] == [str(docx_path)]
    assert pptx_result["pptx_path"] == str(pptx_path)
    assert pptx_result["file_paths"] == [str(pptx_path)]
    assert pptx_result["file_outputs"] == [str(pptx_path)]
    assert pdf_path.read_bytes().startswith(b"%PDF-")

    with ZipFile(docx_path) as zf:
        assert "word/document.xml" in zf.namelist()
    with ZipFile(pptx_path) as zf:
        assert "ppt/presentation.xml" in zf.namelist()

    skill_dir = tmp_path / "skill"
    skill_output_dir = skill_dir / "outputs"
    skill_output_dir.mkdir(parents=True)
    skill_pdf_result = create_pdf("中文测试", output_dir=skill_output_dir, filename="report.pdf")
    stdout = json.dumps({"pdf_path": skill_pdf_result["pdf_path"]})
    assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [
        {"path": "outputs/report.pdf"}
    ]

    extracted = extract_pdf_text(skill_pdf_result["pdf_path"])
    assert extracted["page_count"] >= 1
    assert extracted["text"]


def test_document_helper_output_path_must_stay_under_output_dir(tmp_path):
    output_dir = tmp_path / "outputs"

    with pytest.raises(ValueError, match="OUTPUT_DIR"):
        create_pdf("unsafe", output_dir=output_dir, output_path="../outside.pdf")

    with pytest.raises(ValueError, match="OUTPUT_DIR"):
        create_docx("unsafe", output_dir=output_dir, output_path=tmp_path / "outside.docx")

    pytest.importorskip("pptx")

    result = create_pptx(["ok"], output_dir=output_dir, output_path="nested/safe.pptx")
    assert result["pptx_path"] == str(output_dir / "nested" / "safe.pptx")
    assert (output_dir / "nested" / "safe.pptx").is_file()
