import io
from pathlib import Path

import pytest

from backend.services.creator import upload_context
from backend.services.creator_tool_registry import list_tool_capabilities, resolve_tools_for_skill_plan_entry


def _save(tmp_path, name, data=b"x"):
    old_root = upload_context.UPLOAD_ROOT
    upload_context.UPLOAD_ROOT = tmp_path / "creator_uploads"
    try:
        return upload_context.save_creator_context_upload(
            fileobj=io.BytesIO(data),
            filename=name,
            session_id="session/../unsafe",
            mime_type=None,
        )
    finally:
        upload_context.UPLOAD_ROOT = old_root


def test_image_context_upload_returns_vision_tool_and_no_assets(tmp_path):
    meta = _save(tmp_path, "../photo.png", b"png-bytes")
    assert meta["success"] is True
    assert meta["candidate_tools"] == ["vision_understanding"]
    assert meta["content_kind"] == "image"
    assert meta["asset_decision"] == "unknown"
    assert "assets" not in Path(meta["path"]).parts
    assert meta["original_name"] == "../photo.png"
    assert ".." not in meta["name"]


@pytest.mark.parametrize("filename,tool", [("a.pdf", "pdf_parsing"), ("b.docx", "docx_parsing"), ("c.xlsx", "spreadsheet_read")])
def test_document_context_upload_candidate_tools(tmp_path, filename, tool):
    meta = _save(tmp_path, filename)
    assert tool in meta["candidate_tools"]


def test_context_upload_rejects_unsupported_extension(tmp_path):
    with pytest.raises(ValueError):
        _save(tmp_path, "bad.exe")


def test_context_upload_rejects_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_context, "MAX_CONTEXT_UPLOAD_BYTES", 3)
    with pytest.raises(ValueError):
        _save(tmp_path, "big.txt", b"1234")


def test_vision_understanding_registry_manifest_and_resolution():
    caps = {cap.name: cap for cap in list_tool_capabilities()}
    cap = caps["vision_understanding"]
    assert "analyze_image_with_vision" in cap.helper_imports
    assert cap.usage_policy == "helper_preferred"
    assert any(fn.function_name == "analyze_image_with_vision" and fn.import_path == "backend.services.runtime_tools" for fn in cap.functions)
    result = resolve_tools_for_skill_plan_entry({"path": "scripts/vision.py", "role": "vision_analyzer", "required_tool_slots": ["vision_understanding"]})
    assert "vision_understanding" in result.allowed_tools
    assert "analyze_image_with_vision" in result.allowed_helper_imports
