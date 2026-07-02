import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.creator.common import _creator_tool_context_for_script


def test_repair_tool_rediscovery_adds_pdf_candidate_without_mutating_selected_tools():
    entry = {
        "path": "scripts/read_pdf.py",
        "role": "pdf_parser",
        "runtime": "python",
        "selected_tools": [],
        "required_capabilities": [],
    }
    context = _creator_tool_context_for_script(
        file_path="scripts/read_pdf.py",
        skill_plan_entry=entry,
        blueprint_text="Input: PDF file. Output: extracted text.",
        rediscover_for_repair=True,
        repair_context={
            "structured_failure": {"missing_capability": "PDF 处理 / PDF text extraction"},
            "runtime_contract": {"inputs": ["pdf_path"], "outputs": ["text"]},
            "script_content": "# TODO placeholder\nfrom backend.services.runtime_tools import unknown_helper\n",
        },
    )

    assert "【Plan selected tools】" in context
    assert "【Rediscovered candidate tools for current repair】" in context
    assert "extract_pdf_text" in context
    assert entry["selected_tools"] == []


def test_repair_tool_rediscovery_adds_file_output_for_missing_artifact():
    context = _creator_tool_context_for_script(
        file_path="scripts/write_report.py",
        skill_plan_entry={"path": "scripts/write_report.py", "role": "generic_script", "runtime": "python", "selected_tools": []},
        blueprint_text="Must create a real artifact file and return file_outputs.",
        rediscover_for_repair=True,
        repair_context={
            "structured_failure": {"missing_artifact": "file_outputs missing"},
            "artifact_contract": {"file_fields": ["file_outputs"]},
            "script_content": "print({})",
        },
    )

    assert "file_output" in context
    assert "create_text_file" in context
