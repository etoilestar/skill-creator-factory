import json

import pytest

from backend.routers.chat_models import Message
from backend.routers.sandbox.io_manifest import SandboxChatRequest, build_input_envelope
from backend.routers.sandbox.result_manifest import build_result_manifest
from backend.routers.sandbox.workflow_dataflow import _workflow_context_from_input_envelope


@pytest.mark.asyncio
async def test_instruction_analysis_receives_file_only_envelope(monkeypatch):
    from backend.routers.sandbox.instruction_analysis import _run_instruction_analysis_round

    captured = []

    async def complete(messages, _model):
        captured.extend(messages)
        return json.dumps({
            "intent": "use supplied input", "scope": "input_files", "constraints": [],
            "output_requirements": [], "complexity": "simple", "requires_script_execution": False,
        })

    monkeypatch.setattr("backend.routers.sandbox.instruction_analysis.complete_chat_once", complete)
    envelope = {"user_request": "", "input_files": [{"mime_type": "image/x-future"}], "fields": {"key": "value"}}
    request = SandboxChatRequest(messages=[Message(role="user", content="")])
    result = await _run_instruction_analysis_round(
        body_prompt="skill", request=request, model="model", input_envelope=envelope,
    )
    assert result["intent"] == "use supplied input"
    assert json.dumps(envelope, ensure_ascii=False) in captured[-1]["content"]


def test_file_only_text_preview_and_path_semantics(tmp_path):
    uploaded = tmp_path / "inputs" / "session" / "opaque.data"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_text("readable input", encoding="utf-8")
    request = SandboxChatRequest(
        messages=[Message(role="user", content="")],
        input_files=[{"path": "inputs/session/opaque.data", "mime_type": "application/x-future"}],
    )
    envelope = build_input_envelope(request, tmp_path)
    item = envelope["input_files"][0]
    assert envelope["user_request"] == ""
    assert item["model_access"] == "text_preview"
    assert item["text_preview"] == "readable input"
    assert item["relative_to_input_dir"] == "session/opaque.data"
    assert item["relative_to_input_session_dir"] == "opaque.data"


def test_fields_options_and_mime_image_without_input_suffix(tmp_path):
    image = tmp_path / "inputs" / "session" / "blob"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"binary-image")
    request = SandboxChatRequest(
        messages=[], fields={"subject": "value"}, options={"quality": 2},
        input_files=[{"path": "inputs/session/blob", "mime_type": "image/x-future"}],
    )
    envelope = build_input_envelope(request, tmp_path)
    assert envelope["fields"] == {"subject": "value"}
    assert envelope["options"] == {"quality": 2}
    assert envelope["input_files"][0]["media_family"] == "image/*"
    assert envelope["input_files"][0]["model_access"] == "inline_image"
    assert _workflow_context_from_input_envelope(envelope)["fields"] == {"subject": "value"}


def test_result_manifest_preserves_stdout_and_artifact_mime(tmp_path):
    artifact = tmp_path / "outputs" / "result.json"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    manifest = build_result_manifest({
        "stdout": json.dumps({"unrestricted_business_field": {"value": 1}}),
        "output_files": [{"path": "outputs/result.json", "url": "/download"}],
    }, tmp_path)
    assert manifest["structured_outputs"][0]["data"]["unrestricted_business_field"]["value"] == 1
    assert manifest["artifacts"][0]["mime_type"] == "application/json"
