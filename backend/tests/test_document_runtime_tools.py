import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from backend.services.artifact_validator import validate_stdout_file_outputs
from backend.services.runtime_tools.document_tools import create_csv, create_docx, create_pdf, create_pptx, create_xlsx, extract_pdf_text, read_csv, read_spreadsheet


def test_document_helpers_create_valid_artifacts_and_extract_pdf_text(tmp_path, monkeypatch):
    pytest.importorskip("reportlab")
    pytest.importorskip("docx")
    pytest.importorskip("pptx")
    pytest.importorskip("pypdf")

    output_dir = tmp_path / "doc-outputs"
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))

    pdf_result = create_pdf("中文测试\n第二行", output_dir=output_dir, filename="report.pdf")
    docx_result = create_docx("中文测试", output_dir=output_dir, filename="report.docx")
    pptx_result = create_pptx(["第一页", "第二页"], output_dir=output_dir, filename="report.pptx")
    xlsx_result = create_xlsx(headers=["A", "B"], rows=[{"A": "甲", "B": 2}], output_dir=output_dir, filename="report.xlsx")
    csv_result = create_csv(headers=["A", "B"], rows=[{"A": "甲", "B": 2}], output_dir=output_dir, filename="report.csv")

    pdf_path = output_dir / "report.pdf"
    docx_path = output_dir / "report.docx"
    pptx_path = output_dir / "report.pptx"
    xlsx_path = output_dir / "report.xlsx"
    csv_path = output_dir / "report.csv"

    assert pdf_result["pdf_path"] == str(pdf_path)
    assert pdf_result["file_paths"] == [str(pdf_path)]
    assert pdf_result["file_outputs"] == [str(pdf_path)]
    assert docx_result["docx_path"] == str(docx_path)
    assert docx_result["file_paths"] == [str(docx_path)]
    assert docx_result["file_outputs"] == [str(docx_path)]
    assert pptx_result["pptx_path"] == str(pptx_path)
    assert pptx_result["file_paths"] == [str(pptx_path)]
    assert pptx_result["file_outputs"] == [str(pptx_path)]
    assert xlsx_result["xlsx_path"] == str(xlsx_path)
    assert xlsx_result["file_outputs"] == [str(xlsx_path)]
    assert csv_result["csv_path"] == str(csv_path)
    assert csv_result["file_outputs"] == [str(csv_path)]
    assert pdf_path.read_bytes().startswith(b"%PDF-")

    with ZipFile(docx_path) as zf:
        assert "word/document.xml" in zf.namelist()
    with ZipFile(pptx_path) as zf:
        assert "ppt/presentation.xml" in zf.namelist()
    with ZipFile(xlsx_path) as zf:
        assert "xl/workbook.xml" in zf.namelist()
    assert read_spreadsheet(xlsx_path)["row_count"] == 1
    read_csv_result = read_csv(csv_path)
    assert read_csv_result["row_count"] == 1
    assert read_csv_result["rows"] == [{"A": "甲", "B": "2"}]

    skill_dir = tmp_path / "skill"
    skill_output_dir = skill_dir / "outputs"
    skill_output_dir.mkdir(parents=True)
    skill_pdf_result = create_pdf("中文测试", output_dir=skill_output_dir, filename="report.pdf")
    stdout = json.dumps({"pdf_path": skill_pdf_result["pdf_path"]})
    assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [
        {"path": "outputs/report.pdf"}
    ]

    skill_artifacts = [
        ("docx_path", create_docx("ok", output_dir=skill_output_dir, filename="report.docx")),
        ("pptx_path", create_pptx(["ok"], output_dir=skill_output_dir, filename="report.pptx")),
        ("xlsx_path", create_xlsx(headers=["A"], rows=[["ok"]], output_dir=skill_output_dir, filename="report.xlsx")),
        ("csv_path", create_csv(headers=["A"], rows=[["ok"]], output_dir=skill_output_dir, filename="report.csv")),
    ]
    for field, result in skill_artifacts:
        stdout = json.dumps({field: result[field], "file_outputs": result["file_outputs"]})
        assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [{"path": f"outputs/{Path(result[field]).name}"}]

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


def _write_minimal_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")


def test_artifact_validator_ignores_artifact_metadata_filenames(tmp_path):
    skill_dir = tmp_path / "skill"
    pdf_path = skill_dir / "outputs" / "real.pdf"
    _write_minimal_pdf(pdf_path)

    stdout = json.dumps({
        "pdf_path": str(pdf_path),
        "artifact_metadata": {"options": {"filename": "output.pdf"}},
    })

    assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [
        {"path": "outputs/real.pdf"}
    ]


def test_artifact_validator_allows_other_skill_workspace_subdirectories(tmp_path):
    skill_dir = tmp_path / "skill"
    pdf_path = skill_dir / "reports" / "real.pdf"
    _write_minimal_pdf(pdf_path)

    stdout = json.dumps({"pdf_path": str(pdf_path)})

    assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [
        {"path": "reports/real.pdf"}
    ]


def test_artifact_validator_still_rejects_paths_outside_skill_workspace(tmp_path):
    from backend.services.artifact_validator import FileOutputValidationError

    skill_dir = tmp_path / "skill"
    outside_pdf = tmp_path / "outside.pdf"
    _write_minimal_pdf(outside_pdf)

    stdout = json.dumps({"pdf_path": str(outside_pdf)})

    with pytest.raises(FileOutputValidationError, match="输出路径越界"):
        validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts")
